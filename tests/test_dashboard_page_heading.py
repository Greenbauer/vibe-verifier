"""Page headings in the dashboard (docs/dashboard.md): a title with no sentence describing the page,
a warning on the line under it, and the Pull requests filters beside it."""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PageHeading(unittest.TestCase):
    def test_a_page_title_has_no_description_and_a_warning_takes_the_line_under_it(self):
        source = r'''
const app=require('fs').readFileSync('./dashboard/static/app.js','utf8');
const el=(tag,attrs={},...c)=>({tag,attrs,children:c.flat().filter(v=>v!=null&&v!==false&&v!=='')});
const heading=new Function('el',app.slice(app.indexOf('  function heading('),app.indexOf('  function empty('))+'\nreturn heading;')(el);
const controls={tag:'div',attrs:{class:'toolbar'},children:[]};
console.log(JSON.stringify({quiet:heading('Pull requests',null,controls),
 warned:heading('Pull requests','Repository list is unavailable.',controls),
 plain:heading('Bot usage')}));
'''
        result = subprocess.run(["node", "-e", source], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        parsed = json.loads(result.stdout)
        quiet, warned, plain = parsed["quiet"], parsed["warned"], parsed["plain"]
        # No warning: the title stands alone, the filters beside it.
        self.assertEqual(quiet["children"][0]["children"], [{"tag": "h1", "attrs": {}, "children": ["Pull requests"]}])
        self.assertEqual(quiet["children"][1]["attrs"], {"class": "toolbar"})
        # A warning sits on the line under the title, with its yellow dot.
        title, line = warned["children"][0]["children"]
        self.assertEqual(title["children"], ["Pull requests"])
        self.assertEqual(line["attrs"], {"class": "title-warning", "role": "status"})
        self.assertEqual(line["children"][0]["attrs"], {"class": "warning-dot", "aria-hidden": "true"})
        self.assertEqual(line["children"][1], "Repository list is unavailable.")
        self.assertEqual(warned["children"][1]["attrs"], {"class": "toolbar"})
        # A page without controls is its title alone: no sentence describing the page under it.
        self.assertEqual(plain["children"], [{"tag": "div", "attrs": {"class": "section-title"},
                                              "children": [{"tag": "h1", "attrs": {}, "children": ["Bot usage"]}]}])

    def test_every_page_passes_only_its_title_to_the_heading(self):
        root = ROOT / "dashboard/static"
        calls = {name: re.findall(r"heading\(\"[^\"]+\"[^)]*\)", (root / name).read_text())
                 for name in ("app.js", "capacity.js", "work.js")}
        self.assertEqual(calls, {"app.js": ['heading("Bot usage")'], "capacity.js": ['heading("Capacity")'],
                                 "work.js": ['heading("Work")']})

    def test_controls_follow_their_title_on_the_left_and_a_phone_stacks_them_in_three_rows(self):
        css = (ROOT / "dashboard/static/styles.css").read_text()
        def rule(selector, within=css):
            start = within.index("\n" + selector + " {") + 1
            return within[start:within.index("}", start)]
        # Wide: the search is a fixed 260px and every control follows the title from the left.
        self.assertIn("justify-content: flex-start", rule(".section-heading:has(.toolbar)"))
        self.assertIn("display: contents", rule(".section-heading .toolbar"))
        self.assertIn("flex: 0 1 260px", rule('.toolbar input[type="search"]'))
        # The Capacity range buttons follow their panel title the same way.
        self.assertIn("justify-content: flex-start", rule(".panel-title:has(.range-buttons)"))
        # No wide-screen rule moves the Bot usage failure buttons to the right of their title.
        self.assertNotIn(".failures .failure-buttons", css)
        # Phone: the search fills the title's line, each group takes a full row, the menus split it.
        phone = css[css.index("@media (max-width: 650px)"):]
        phone = phone[:phone.index("\n}\n")]
        self.assertIn("flex: 1 1 0", rule('  .section-heading .toolbar input[type="search"]', phone))
        self.assertIn("flex: 1 1 100%", rule("  .section-heading .toolbar-group", phone))
        self.assertIn("flex: 1 1 0; min-width: 0", rule("  .toolbar-group select", phone))

if __name__ == "__main__":
    unittest.main()
