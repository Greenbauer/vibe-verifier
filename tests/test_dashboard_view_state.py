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

    def test_url_hash_names_every_view_and_pull_request_and_rejects_anything_else(self):
        result = self.node(r'''
const {parseRoute:parse,routeHash:hash}=require('./dashboard/static/helpers.js');
const routes=[{view:'prs',selected:null},{view:'usage',selected:null},{view:'capacity',selected:null},
  {view:'prs',selected:'Greenbauer/vibe-verifier#12'},{view:'prs',selected:'my-org/repo.name_2#7'}];
console.log(JSON.stringify({hashes:routes.map(hash),round:routes.map(route=>parse(hash(route))),routes,
  invalid:['','#','#/','#/other','#usage','#/usage/x','#/pr/a/b','#/pr/a/b/c','#/pr/a/b/1/2','#/pr/a%2Fb/c/1',
    '#/pr/<x>/b/1',null,undefined].map(parse)}));
''')
        self.assertEqual(result['hashes'], ['#/prs', '#/usage', '#/capacity', '#/pr/Greenbauer/vibe-verifier/12',
                                            '#/pr/my-org/repo.name_2/7'])
        self.assertEqual(result['round'], result['routes'])
        self.assertTrue(all(row == {'view': 'prs', 'selected': None} for row in result['invalid']))

    def test_storage_is_owner_scoped_and_disabled_storage_does_not_break_navigation(self):
        result = self.node(r'''
const fs=require('fs'),VV=require('./dashboard/static/helpers.js');
const app=fs.readFileSync('./dashboard/static/app.js','utf8');
const functions=app.slice(app.indexOf('  function restoreView('),app.indexOf('  function announce('));
const state=VV.restoreViewState(null),snapshot={owner:'Greenbauer'};
const entries=new Map(),storage={getItem:key=>entries.get(key)||null,setItem:(key,value)=>entries.set(key,value)};
const build=new Function('VV','state','snapshot','sessionStorage',functions+'return {restoreView,rememberView};');
const api=build(VV,state,snapshot,storage);
Object.assign(state,{view:'usage',selected:null,query:'fix',failureBot:'qae-1'}); api.rememberView();
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
const state={view:'prs',selected:null},location={hash:'#/prs'},renders=[],replaced=[];
const content={focus(){}},window={scrollTo(){}};
const history={replaceState:(data,title,hash)=>{replaced.push(hash);location.hash=hash;}};
const render=()=>renders.push(VV.routeHash(state));
const api=new Function('VV','state','location','history','content','window','render',functions+'return {go,show};')(
  VV,state,location,history,content,window,render);
api.go('usage'); const clicked={hash:location.hash,renders:renders.length};
api.show(); api.go('prs','Greenbauer/vibe-verifier#12'); api.show();
location.hash='#/usage'; api.show();
api.go('usage');
const known={state:{...state},replaced:[...replaced]};
location.hash='#/nonsense'; api.show();
console.log(JSON.stringify({clicked,renders,known,state,hash:location.hash,replaced}));
''')
        # A click only sets the hash; the browser records it and fires hashchange, which draws it.
        self.assertEqual(result['clicked'], {'hash': '#/usage', 'renders': 0})
        # Back from the PR to Bot usage redraws Bot usage; clicking the view already shown redraws in place.
        self.assertEqual(result['renders'][:4], ['#/usage', '#/pr/Greenbauer/vibe-verifier/12', '#/usage', '#/usage'])
        self.assertEqual(result['known'], {'state': {'view': 'usage', 'selected': None}, 'replaced': []})
        # A hash typed by hand that names no view shows pull requests, and the address is corrected
        # in place rather than adding a history entry.
        self.assertEqual(result['state'], {'view': 'prs', 'selected': None})
        self.assertEqual((result['hash'], result['replaced']), ('#/prs', ['#/prs']))

    def test_missing_selected_pr_survives_loading_and_can_return_to_list(self):
        result = self.node(r'''
const fs=require('fs');
const app=fs.readFileSync('./dashboard/static/app.js','utf8');
const source=app.slice(app.indexOf('  function renderDetail()'),app.indexOf('  function quota('));
const state={view:'prs',selected:'Greenbauer/vibe-verifier#12'},snapshot={github:{refreshing:true}};
let children,rendered=false;
const content={replaceChildren:(...items)=>children=items,focus(){}};
const el=(tag,attrs,...items)=>({tag,attrs,items}),empty=(title,detail)=>({title,detail});
const go=view=>{state.selected=null;rendered=view==='prs';};
const run=new Function('state','snapshot','content','flattenPulls','el','empty','go',source+'renderDetail();');
const args=[state,snapshot,content,()=>[],el,empty,go];
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
