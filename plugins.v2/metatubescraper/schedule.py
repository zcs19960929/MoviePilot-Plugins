"""Translate saved settings into MoviePilot's existing scheduler contract."""
from decimal import Decimal, InvalidOperation


def _value(value, default=""):
    if isinstance(value, dict):
        value = value.get("value", value.get("title"))
    return str(value).strip() if value is not None else default


def _integer(value, label, minimum, maximum=None):
    try:
        number = Decimal(_value(value))
        if (not number.is_finite() or number != number.to_integral_value()
                or number < minimum or (maximum is not None and number > maximum)):
            raise ValueError
        return int(number)
    except (InvalidOperation, ValueError, OverflowError):
        limit = f"{minimum}–{maximum} 的整数" if maximum is not None else "正整数"
        raise ValueError(f"{label}须为{limit}") from None


def build_schedule(config):
    """Return trigger alias/kwargs, None for manual, or a clear validation error.

    Keep trigger aliases rather than prebuilt trigger objects: the host supplies
    its scheduler timezone when it constructs the actual trigger.
    """
    mode = _value(config.get("schedule_mode")) or "daily"
    if mode == "manual":
        return None
    if mode in ("daily", "weekly"):
        kwargs = {
            "hour": _integer(config.get("cron_hour", 4), "执行小时", 0, 23),
            "minute": _integer(config.get("cron_minute", 30), "执行分钟", 0, 59),
        }
        if mode == "weekly":
            day = _value(config.get("schedule_weekday", "mon")).lower()
            if day not in ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "0", "1", "2", "3", "4", "5", "6"):
                raise ValueError("请选择有效的执行星期")
            kwargs["day_of_week"] = day
        return {"trigger": "cron", "kwargs": kwargs}
    if mode == "interval":
        # APScheduler/timedelta cannot represent arbitrarily large user input.
        minutes = _integer(config.get("schedule_interval", 60), "间隔分钟数", 1, 525600)
        return {"trigger": "interval", "kwargs": {"minutes": minutes}}
    if mode == "cron":
        expression = _value(config.get("cron_expression"))
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError("Cron 须为五段：分 时 日 月 星期，例如 30 4 * * *")
        try:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger.from_crontab(" ".join(fields))
        except ImportError:
            raise ValueError("宿主缺少 APScheduler，无法校验 Cron") from None
        except ValueError as error:
            raise ValueError(f"Cron 表达式无效：{error}；星期为 0=周一、6=周日，建议使用 mon–sun") from None
        return {"trigger": "cron", "kwargs": dict(zip(("minute", "hour", "day", "month", "day_of_week"), fields))}
    raise ValueError("请选择有效的调度方式")
