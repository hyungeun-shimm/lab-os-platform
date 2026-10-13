const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const mobile = fs.readFileSync('modules/scheduler/mobile.html', 'utf8');
const desktop = fs.readFileSync('modules/scheduler/scheduler.html', 'utf8');
function extract(source, start, end) {
  return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
}
async function main() {
  const elements = Object.fromEntries(Object.entries({fDate:'2026-10-14',fDateEnd:'2026-10-14',fStart:'09:00',fEnd:'10:00',
    fTitle:'Edited',fCat:'experiments',fMice:'TEST01',fNotes:'Updated note',fSub:'Behavior',fProject:'project',fCohort:'I',
    fAssignee:'team',fStatus:'done'}).map(([id,value])=>[id,{value}]));
  elements.fAllDay={checked:false}; elements.evSave={disabled:false};
  const original={id:'event',status:'done',project_slug:'project',cohort:'I',assignee:'team',subcategory2:'OKR',
    recur:{freq:'weekly',until:'2027-01-01'},owner_id:'owner',delay_reason:'keep',reschedule_to:'2026-10-15'};
  let saved;
  const context={document:{getElementById:id=>elements[id]},state:{events:[original],editing:'event'},Date,
    readRes:()=>'',evMsg:()=>{},hideSheet:()=>{},load:async()=>{},render:()=>{},api:async(path,opts)=>{saved=JSON.parse(opts.body);}};
  vm.createContext(context);
  vm.runInContext(extract(mobile,'async function evSave(){','async function evDelete(){'),context);
  await context.evSave();
  for(const key of ['recur','subcategory2','owner_id','delay_reason','reschedule_to']) assert.deepEqual(saved[key],original[key]);
  assert.equal(saved.status,'done'); assert.equal(saved.project_slug,'project');
  const scheduler={state:{events:[{id:'one',title:'OKR',category:'experiments',mice:['TEST01'],cohort:'I',status:'done'},
    {id:'two',title:'Slice',category:'experiments',mice:['TEST02'],cohort:'J',status:'planned'}],catFilter:new Set(['experiments']),
    projectShowAll:true,assigneeShowAll:true,cohortShowAll:true,mineOnly:false,statusFilter:'',searchQuery:'test01'},
    projectNameOf:()=>'',memberById:()=>null,baseCohort:x=>x};
  vm.createContext(scheduler);
  vm.runInContext(extract(desktop,'function visibleEvents() {','function memberById'),scheduler);
  assert.equal(scheduler.visibleEvents().length,1); assert.equal(scheduler.visibleEvents()[0].id,'one');
  scheduler.state.searchQuery=''; scheduler.state.statusFilter='planned';
  assert.equal(scheduler.visibleEvents()[0].id,'two');
  scheduler.state.statusFilter='';scheduler.state.cohortShowAll=false;scheduler.state.cohortFilter=new Set(['I']);
  assert.equal(scheduler.visibleEvents()[0].id,'one');
  console.log('Frontend regressions passed: mobile metadata preservation, mouse search, status/cohort filters');
}
main().catch(error=>{console.error(error);process.exit(1);});
