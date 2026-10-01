"""Refresh restores navigation, without trusting stored values or changing owners."""

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ViewState(unittest.TestCase):
    def node(self, source):
        result = subprocess.run(["node", "-e", source], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_all_navigation_preferences_round_trip_and_invalid_values_default(self):
        result = self.node(r'''
const {restoreViewState:restore}=require('./dashboard/static/helpers.js');
const saved={view:'prs',selected:'Greenbauer/vibe-verifier#12',query:'fix',repository:'Greenbauer/vibe-verifier',
  subscribed:true,attention:true,usageRange:'7d',failureBot:'qae-1'};
console.log(JSON.stringify({saved,restored:restore(JSON.stringify(saved)),
  views:['usage','capacity'].map(view=>restore(JSON.stringify({...saved,view}))),
  invalid:[null,'broken','null','42',JSON.stringify({view:'other',selected:42,query:[],repository:false,
    subscribed:'yes',attention:1,usageRange:'forever',failureBot:{}})].map(restore),defaults:restore(null)}));
''')
        self.assertEqual(result['restored'], result['saved'])
        self.assertEqual([row['view'] for row in result['views']], ['usage', 'capacity'])
        self.assertTrue(all(row['selected'] is None for row in result['views']))
        self.assertTrue(all(row == result['defaults'] for row in result['invalid']))

    def test_storage_is_owner_scoped_and_disabled_storage_does_not_break_navigation(self):
        result = self.node(r'''
const fs=require('fs'),VV=require('./dashboard/static/helpers.js');
const app=fs.readFileSync('./dashboard/static/app.js','utf8');
const functions=app.slice(app.indexOf('  function restoreView('),app.indexOf('  function announce('));
const state=VV.restoreViewState(null),snapshot={owner:'Greenbauer'};
const entries=new Map(),storage={getItem:key=>entries.get(key)||null,setItem:(key,value)=>entries.set(key,value)};
const build=new Function('VV','state','snapshot','sessionStorage',functions+'return {restoreView,rememberView};');
const api=build(VV,state,snapshot,storage);
Object.assign(state,{view:'usage',usageRange:'7d',failureBot:'qae-1'}); api.rememberView();
state.view='prs'; api.restoreView('Greenbauer'); const restored={...state};
api.restoreView('another-owner'); const other={...state};
const denied=build(VV,state,snapshot,{getItem(){throw Error('disabled');},setItem(){throw Error('disabled');}});
denied.restoreView('Greenbauer'); denied.rememberView();
console.log(JSON.stringify({restored,other,keys:[...entries.keys()]}));
''')
        self.assertEqual(result['restored']['view'], 'usage')
        self.assertEqual(result['restored']['usageRange'], '7d')
        self.assertEqual(result['restored']['failureBot'], 'qae-1')
        self.assertEqual(result['other']['view'], 'prs')
        self.assertEqual(result['keys'], ['vv-dashboard-view:Greenbauer'])

    def test_missing_selected_pr_survives_loading_and_can_return_to_list(self):
        result = self.node(r'''
const fs=require('fs');
const app=fs.readFileSync('./dashboard/static/app.js','utf8');
const source=app.slice(app.indexOf('  function renderDetail()'),app.indexOf('  function quota('));
const state={view:'prs',selected:'Greenbauer/vibe-verifier#12'},snapshot={github:{refreshing:true}};
let children,rendered=false;
const content={replaceChildren:(...items)=>children=items,focus(){}};
const el=(tag,attrs,...items)=>({tag,attrs,items}),empty=(title,detail)=>({title,detail});
const run=new Function('state','snapshot','content','flattenPulls','el','empty','render','window',source+'renderDetail();');
const args=[state,snapshot,content,()=>[],el,empty,()=>{rendered=true;},{scrollTo(){}}];
run(...args); const loading={selected:state.selected,title:children[1].title};
snapshot.github.refreshing=false; run(...args); const missing={selected:state.selected,title:children[1].title};
children[0].attrs.onclick();
console.log(JSON.stringify({loading,missing,back:state.selected,rendered}));
''')
        self.assertEqual(result['loading'], {'selected': 'Greenbauer/vibe-verifier#12', 'title': 'Loading pull request'})
        self.assertEqual(result['missing'], {'selected': 'Greenbauer/vibe-verifier#12', 'title': 'Pull request unavailable'})
        self.assertIsNone(result['back'])
        self.assertTrue(result['rendered'])


if __name__ == '__main__':
    unittest.main()
