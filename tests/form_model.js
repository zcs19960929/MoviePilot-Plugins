// Exercise the plugin schema using the host FormRender's expression/event contract.
const fs = require('node:fs');
const { form, model, changes = [], asyncBrowse, cookieCheck, directoryFlow, directoryFailure, pickerFlow } = JSON.parse(fs.readFileSync(0, 'utf8'));
const expression = value => typeof value === 'string' && value.startsWith('{{') && value.endsWith('}}');
const evaluate = value => expression(value)
  ? new Function('model', `with(model) { return ${value.slice(2, -2)} }`)(model)
  : value;
function visit(nodes, visible = true, fields = {}) {
  for (const node of nodes) {
    const props = node.props || {};
    const shown = visible && (props.show === undefined || Boolean(evaluate(props.show)))
      && (node.component !== 'VDialog' || Boolean(model[props.model]));
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
  label=({'读取目录':'刷新','添加为扫描目录':'选择当前目录'})[label] || label;
  for (const node of nodes) {
    if (node.text === label) return node;
    if(label==='上一级' && node.component==='VList' && node.props?.['onUpdate:selected'])
      return {props:{onClick:`() => (${node.props['onUpdate:selected']})(['__parent__'])`}};
    const child = button(node.content || [], label);
    if (child) return child;
  }
}
function findComponent(nodes,name) {
  for(const node of nodes) {
    if(node.component===name)return node;
    const child=findComponent(node.content||[],name);if(child)return child;
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
if(pickerFlow) {
  const pending=[];
  global.window={MoviePilotAPI:Object.fromEntries(['get','post'].map(method=>[method,()=>new Promise((resolve,reject)=>pending.push({resolve,reject}))]))};
  const open=execute(button(form,'选择目录').props.onClick);
  intermediate={loading:model.dir_loading,unchanged:model.scan_dirs};
  if(pickerFlow.cancelPending) {
    if(pickerFlow.dismiss) execute(find(form,'dir_picker_open').props['onUpdate:modelValue'],false);
    else execute(button(form,'取消').props.onClick);
    pending.shift().resolve({code:0,path:'id:9',parent:'id:0',display_path:'/迟到目录',data:[]});
    await open;
  } else {
    pending.shift().resolve({code:0,path:'id:7',parent:'id:0',display_path:'/电影',data:[]});
    await open;snapshots.push(visit(form));
    if(pickerFlow.cancel)execute(button(form,'取消').props.onClick);
    else execute(button(form,'选择当前目录').props.onClick);
  }
  snapshots.push(visit(form));
}
if(directoryFailure) {
  global.window={MoviePilotAPI:{get:async()=>{throw new Error('directory request failed');}}};
  const source=directoryFailure.action==='up'
    ? button(form,'上一级').props.onClick
    : findComponent(form,'VList').props['onUpdate:selected'];
  if(directoryFailure.action!=='addOnly') await execute(source,'id:8');
  execute(button(form,'添加为扫描目录').props.onClick);
  snapshots.push(visit(form));
}
if(directoryFlow) {
  const replies=[
    {code:0,path:'id:0',parent:'id:0',display_path:'/',data:[{name:'电影',path:'id:7',type:'dir',display_path:'/电影'}]},
    {code:0,path:'id:7',parent:'id:0',display_path:'/电影',data:[{name:'中文合集',path:'id:8',type:'dir',display_path:'/电影/中文合集'}]},
    {code:0,path:'id:7',parent:'id:0',display_path:'/电影',data:[]},
    {code:0,path:'id:0',parent:'id:0',display_path:'/',data:[{name:'电影',path:'id:7',type:'dir',display_path:'/电影'}]},
  ];
  global.window={MoviePilotAPI:{get:async()=>replies.shift()}};
  await execute(button(form,'选择目录').props.onClick);
  await execute(findComponent(form,'VList').props['onUpdate:selected'],['id:7']);
  execute(button(form,'添加为扫描目录').props.onClick);
  snapshots.push(visit(form));
  intermediate={display:model.browse_display_path,ids:model.scan_dirs,status:model.dir_error};
  await execute(button(form,'选择目录').props.onClick);
  await execute(button(form,'上一级').props.onClick);
  snapshots.push(visit(form));
  if(directoryFlow.edit) {
    execute(find(form,'browse_display_path').props['onUpdate:modelValue'],'/新电影');
    execute(find(form,'scan_dirs').props['onUpdate:modelValue'],[{title:'/电影',value:'id:7'},'/新电影']);
    snapshots.push(visit(form));
  }
}
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
    ? findComponent(form, 'VList').props['onUpdate:selected']
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
    if (asyncBrowse.editCookie) execute(find(form, 'cookie115').props['onUpdate:modelValue'], 'changed fixture');
    else {
      execute(find(form, 'storage').props['onUpdate:modelValue'], 'local');
      if (asyncBrowse.awayBack) execute(find(form, 'storage').props['onUpdate:modelValue'], '115_direct');
    }
    if (asyncBrowse.reject) pending[0].reject(new Error('stale failure'));
    else pending[0].resolve({ code: 0, path: 'id:99', parent: 'id:0', data: [{ name: 'stale', path: 'id:100', type: 'dir' }] });
    await first;
  }
}
process.stdout.write(JSON.stringify({ snapshots, model, intermediate }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
