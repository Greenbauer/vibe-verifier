"""Refresh, back and forward restore navigation, without trusting stored values or changing owners."""

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
const saved={query:'fix',repository:'Greenbauer/vibe-verifier',subscribed:true,attention:true,failureBot:'qae-1'};
console.log(JSON.stringify({saved,restored:restore(JSON.stringify(saved)),
  legacy:restore(JSON.stringify({...saved,usageRange:'7d',view:'usage',selected:'Greenbauer/vibe-verifier#12'})),
  invalid:[null,'broken','null','42',JSON.stringify({view:'other',selected:42,query:[],repository:false,
    subscribed:'yes',attention:1,usageRange:'forever',failureBot:{}})].map(restore),defaults:restore(null)}));
''')
        self.assertEqual(result['restored'], result['saved'])
        # A tab saved by an older dashboard still restores its filters; its view and usage period
        # are ignored, because the URL now says where you are.
        self.assertEqual(result['legacy'], result['saved'])
        self.assertTrue(all(row == result['defaults'] for row in result['invalid']))

    def test_url_hash_names_every_view_and_rejects_anything_else(self):
        result = self.node(r'''
const {parseRoute:parse,routeHash:hash}=require('./dashboard/static/helpers.js');
const routes=[{view:'prs'},{view:'usage'},{view:'capacity'}];
console.log(JSON.stringify({hashes:routes.map(hash),round:routes.map(route=>parse(hash(route))),routes,
  invalid:['','#','#/','#/other','#usage','#/usage/x','#/pr/Greenbauer/vibe-verifier/12','#/pr/a/b','#/pr/a/b/c',
    '#/pr/a/b/1/2','#/pr/a%2Fb/c/1','#/pr/<x>/b/1',null,undefined].map(parse)}));
''')
        self.assertEqual(result['hashes'], ['#/prs', '#/usage', '#/capacity'])
        self.assertEqual(result['round'], result['routes'])
        self.assertTrue(all(row == {'view': 'prs'} for row in result['invalid']))

    def test_storage_is_owner_scoped_and_disabled_storage_does_not_break_navigation(self):
        result = self.node(r'''
const fs=require('fs'),VV=require('./dashboard/static/helpers.js');
const app=fs.readFileSync('./dashboard/static/app.js','utf8');
const functions=app.slice(app.indexOf('  function restoreView('),app.indexOf('  function announce('));
const state=VV.restoreViewState(null),snapshot={owner:'Greenbauer'};
const entries=new Map(),storage={getItem:key=>entries.get(key)||null,setItem:(key,value)=>entries.set(key,value)};
const build=new Function('VV','state','snapshot','sessionStorage',functions+'return {restoreView,rememberView};');
const api=build(VV,state,snapshot,storage);
Object.assign(state,{view:'usage',query:'fix',failureBot:'qae-1'}); api.rememberView();
state.query=''; api.restoreView('Greenbauer'); const restored={...state};
api.restoreView('another-owner'); const other={...state};
const denied=build(VV,state,snapshot,{getItem(){throw Error('disabled');},setItem(){throw Error('disabled');}});
denied.restoreView('Greenbauer'); denied.rememberView();
console.log(JSON.stringify({restored,other,keys:[...entries.keys()],stored:JSON.parse(entries.get('vv-dashboard-view:Greenbauer'))}));
''')
        self.assertEqual(result['restored']['query'], 'fix')
        self.assertEqual(result['restored']['failureBot'], 'qae-1')
        self.assertEqual(result['other']['query'], '')
        self.assertEqual(result['keys'], ['vv-dashboard-view:Greenbauer'])
        # The view is the URL's to record, so storage never holds a second copy that could disagree.
        self.assertNotIn('view', result['stored'])
        self.assertNotIn('selected', result['stored'])

    def test_clicks_add_history_entries_and_back_and_forward_redraw_from_the_url(self):
        result = self.node(r'''
const fs=require('fs'),VV=require('./dashboard/static/helpers.js');
const app=fs.readFileSync('./dashboard/static/app.js','utf8');
const functions=app.slice(app.indexOf('  function go('),app.indexOf('  function announce('));
const state={view:'prs'},location={hash:'#/prs'},renders=[],replaced=[];
const content={focus(){}},window={scrollTo(){}};
const history={replaceState:(data,title,hash)=>{replaced.push(hash);location.hash=hash;}};
const render=()=>renders.push(VV.routeHash(state));
const api=new Function('VV','state','location','history','content','window','render',functions+'return {go,show};')(
  VV,state,location,history,content,window,render);
api.go('usage'); const clicked={hash:location.hash,renders:renders.length};
api.show(); api.go('capacity'); api.show();
location.hash='#/usage'; api.show();
api.go('usage');
const known={state:{...state},replaced:[...replaced]};
location.hash='#/pr/Greenbauer/vibe-verifier/12'; api.show();
console.log(JSON.stringify({clicked,renders,known,state,hash:location.hash,replaced}));
''')
        # A click only sets the hash; the browser records it and fires hashchange, which draws it.
        self.assertEqual(result['clicked'], {'hash': '#/usage', 'renders': 0})
        # Back from Capacity to Bot usage redraws Bot usage; clicking the view already shown redraws in place.
        self.assertEqual(result['renders'][:4], ['#/usage', '#/capacity', '#/usage', '#/usage'])
        self.assertEqual(result['known'], {'state': {'view': 'usage'}, 'replaced': []})
        # An older pull-request hash names no view. It shows the list, and the address is corrected
        # in place rather than adding a history entry.
        self.assertEqual(result['state'], {'view': 'prs'})
        self.assertEqual((result['hash'], result['replaced']), ('#/prs', ['#/prs']))


if __name__ == '__main__':
    unittest.main()
