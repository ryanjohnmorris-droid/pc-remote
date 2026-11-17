"""Bundle source/server.py + source/static/ into the one-file ../server.py"""
import base64, json, pathlib

here = pathlib.Path(__file__).parent
s = (here / "server.py").read_text(encoding="utf-8")
names = ["index.html", "manifest.json", "icon.svg", "apple-touch-icon.png", "icon-192.png", "icon-512.png"]
assets = {n: base64.b64encode((here / "static" / n).read_bytes()).decode() for n in names}

start = s.index("    # --- static files (build_single.py replaces this method) ---")
end = s.index("    # --- end static files ---") + len("    # --- end static files ---")
s = s[:start] + '''    # --- static files (built in) ---
    def _static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        if name not in ASSETS:
            return self._reply(404, b"not found")
        self._reply(200, base64.b64decode(ASSETS[name]),
                    TYPES.get(Path(name).suffix, "application/octet-stream"))''' + s[end:]
s = s.replace('STATIC = HERE / "static"\n', "")
s = s.replace("Run on the PC:   python server.py", "Run on the PC:   double-click this file")
s = s.replace('\n\nif __name__ == "__main__":', "\n\n# Phone app files (built in, so this is the only file you need)\nASSETS = "
              + json.dumps(assets, indent=1) + '\n\n\nif __name__ == "__main__":')
(here.parent / "server.py").write_text(s, encoding="utf-8")
print("built server.py", len(s), "bytes")
