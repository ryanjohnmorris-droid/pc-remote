"""Bundle source/server.py + source/static/ into the one-file ../server.py"""
import base64, json, pathlib
here = pathlib.Path(__file__).parent
s = (here / "server.py").read_text()
assets = {n: base64.b64encode((here / "static" / n).read_bytes()).decode()
          for n in ["index.html", "manifest.json", "icon.svg", "apple-touch-icon.png"]}
old_get = s[s.index("    def do_GET(self):"):s.index("    def do_POST(self):")]
s = s.replace(old_get, '''    def do_GET(self):
        path = urlparse(self.path).path
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        if name not in ASSETS:
            return self._reply(404, b"not found")
        self._reply(200, base64.b64decode(ASSETS[name]),
                    TYPES.get(Path(name).suffix, "application/octet-stream"))

''')
s = s.replace('STATIC = HERE / "static"\n', "").replace("import json\n", "import base64\nimport json\n", 1)
s = s.replace("Run on the PC:   python server.py", "Run on the PC:   double-click this file")
s = s.replace('\n\nif __name__ == "__main__":', "\n\n# Phone app files (built in, so this is the only file you need)\nASSETS = "
              + json.dumps(assets, indent=1) + '\n\n\nif __name__ == "__main__":')
(here.parent / "server.py").write_text(s)
print("built server.py", len(s), "bytes")
