#!/usr/bin/env python3
"""Builds tools/serial_bridge/crosspoint-serial.html: the real CrossPoint file
manager page, with CSS/icons inlined and the network layer replaced by shim.js
(Web Serial). Open the result in Chrome or Edge. Run after changing the web UI."""
import gzip, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_DIR = os.path.join(HERE, "..", "..", "src", "network", "html")


def asset(symbol):
    for root, _, files in os.walk(HTML_DIR):
        for f in files:
            if not f.endswith(".generated.h"):
                continue
            text = open(os.path.join(root, f), encoding="utf-8").read()
            if f"char {symbol}[]" not in text:
                continue
            body = text.split(f"char {symbol}[]", 1)[1].split("};", 1)[0]
            raw = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", body))
            return (gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8")
    raise SystemExit("asset not found: " + symbol)


def main():
    page = asset("FilesPageHtml")
    css = asset("webCss")
    icons = asset("iconsSvg")
    embedded = {
        "/js/jszip.min.js": asset("jszip_minJs"),
        "/js/epub-optimizer.js": asset("epub_optimizerJs"),
    }
    shim = open(os.path.join(HERE, "shim.js"), encoding="utf-8").read()
    page = page.replace('<link rel="stylesheet" href="/assets/web.css">', "<style>" + css + "</style>")
    page = re.sub(r'<link[^>]*href="/assets/web\.css"[^>]*>', lambda m: "<style>" + css + "</style>", page)
    page = page.replace("/assets/icons.svg#", "#")
    page = re.sub(r'<script src="/js/plugin-host\.js"></script>', "", page)
    boot = ("<script>window.__cpxScripts=" + json.dumps(embedded).replace("</", "<" + chr(92) + "/") + ";</script>"
            "<script>" + shim.replace("</script", "<" + chr(92) + "/script") + "</script>")
    page = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + boot + '<div style="display:none">' + icons + "</div>",
                  page, count=1)
    import base64
    logo = "data:image/svg+xml;base64," + base64.b64encode(asset("crosspoint_iconSvg").encode()).decode()
    page = page.replace("/assets/crosspoint-icon.svg", logo)
    out = os.path.join(HERE, "crosspoint-serial.html")
    open(out, "w", encoding="utf-8").write(page)
    print("wrote", out, len(page), "bytes")


if __name__ == "__main__":
    main()
