// Exercise the plugin schema using the host FormRender's expression/event contract.
const fs = require('node:fs');
const { form, model, changes = [], asyncBrowse, cookieCheck } = JSON.parse(fs.readFileSync(0, 'utf8'));
const expression = value => typeof value === 'string' && value.startsWith('{{') && value.endsWith('}}');
const evaluate = value => expression(value)
  ? new Function('model', `with(model) { return ${value.slice(2, -2)} }`)(model)
  : value;
function visit(nodes, visible = true, fields = {}) {
  for (const node of nodes) {
    const props = node.props || {};
    const shown = visible && (props.show === undefined || Boolean(evaluate(props.show)));
    if (shown && props.model) {
      fields[props.model] = Object.fromEntries(Object.entries(props)
        .filter(([key]) => !key.startsWith('on')).map(([key, value]) => [key, evaluate(value)]));
    }
    visit(node.content || [], shown, fields);
  }
  return fields;
}
function find(nodes, name) {
  for (const node of nodes) {
    if (node.props?.model === name) return node;
    const child = find(node.content || [], name);
    if (child) return child;
  }
}
function button(nodes, label) {
  for (const node of nodes) {
    if (node.text === label) return node;
    const child = button(node.content || [], label);
    if (child) return child;
  }
}
const execute = (source, value) => new Function('model', 'event', `with(model) { return (${source})(event); }`)(model, value);
async function main() {
const snapshots = [visit(form)];
for (const { field, value } of changes) {
  const node = find(form, field);
  const handler = node.props['onUpdate:modelValue'];
  if (handler) execute(handler, value);
  else model[field] = value;
  snapshots.push(visit(form));
}
let intermediate;
if (cookieCheck) {
  const pending=[];
  global.window={MoviePilotAPI:{get:()=>new Promise((resolve,reject)=>pending.push({resolve,reject}))}};
  const source=button(form,'检查已保存的 115 Cookie').props.onClick;
  const task=execute(source);
  const second=execute(source); // The loading button must not issue a duplicate request.
  intermediate={loading:model.cookie115_checking,requests:pending.length};
  if(cookieCheck.switchMode) execute(find(form,'storage').props['onUpdate:modelValue'],'local');
  if(cookieCheck.editCookie) execute(find(form,'cookie115').props['onUpdate:modelValue'],'updated fixture');
  for(const request of pending) {
    if(cookieCheck.reject)request.reject(new Error('network error'));
    else request.resolve({data:{code:0,msg:'115 Cookie 可用'}});
  }
  await task;
  await second;
  snapshots.push(visit(form));
}
if (asyncBrowse) {
  const pending = [];
  global.window = { MoviePilotAPI: Object.fromEntries(['get', 'post'].map(method =>
    [method, () => new Promise((resolve, reject) => pending.push({ resolve, reject }))])) };
  const source = asyncBrowse.action === 'enter'
    ? find(form, 'pick_dir').props['onUpdate:modelValue']
    : button(form, asyncBrowse.action === 'up' ? '上一级' : '读取目录').props.onClick;
  const first = execute(source, 'id:7');
  if (asyncBrowse.overlap) {
    const second = execute(source, 'id:8');
    pending[0].resolve({ code: 0, path: 'id:99', parent: 'id:0', data: [{ name: 'stale', path: 'id:100', type: 'dir' }] });
    await first;
    intermediate = { loading: model.dir_loading, path: model.browse_path, names: model.dir_items.map(x => x.title) };
    pending[1].resolve({ code: 0, path: 'id:88', parent: 'id:0', data: [{ name: 'fresh', path: 'id:89', type: 'dir' }] });
    await second;
  } else {
    execute(find(form, 'storage').props['onUpdate:modelValue'], 'local');
    if (asyncBrowse.awayBack) execute(find(form, 'storage').props['onUpdate:modelValue'], '115_direct');
    if (asyncBrowse.reject) pending[0].reject(new Error('stale failure'));
    else pending[0].resolve({ code: 0, path: 'id:99', parent: 'id:0', data: [{ name: 'stale', path: 'id:100', type: 'dir' }] });
    await first;
  }
}
process.stdout.write(JSON.stringify({ snapshots, model, intermediate }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
