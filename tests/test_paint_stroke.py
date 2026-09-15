#!/usr/bin/env python3
"""Integration test for paint_stroke, list_brushes and sample_color.

Requires the GIMP MCP plugin running on localhost:9877.
Exits 0 on pass, 1 on fail.
"""
import json
import socket
import sys

HOST, PORT = '127.0.0.1', 9877
WIDTH, HEIGHT = 400, 320


# ── transport ─────────────────────────────────────────────────────────────

def send(msg, timeout=30):
    """Send one JSON message to the MCP socket and return the reply."""
    s = socket.socket()
    s.settimeout(timeout)
    s.connect((HOST, PORT))
    s.send(json.dumps(msg).encode() + b'\n')
    buf = b''
    while True:
        try:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            try:
                json.loads(buf.decode())
                break
            except json.JSONDecodeError:
                continue
        except socket.timeout:
            break
    s.close()
    try:
        return json.loads(buf.decode().strip())
    except json.JSONDecodeError:
        return {'status': 'error', 'error': 'parse: ' + buf.decode()[:200]}


def cmd(t, params=None):
    """Send a {type, params} command."""
    return send({'type': t, 'params': params or {}})


def fail(msg):
    """Abort with a failure banner on stderr."""
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def results_of(r, what):
    """Return the results dict of a successful reply, or fail naming the call."""
    if r.get('status') != 'success':
        fail(f"{what}: {r.get('error', '')}")
    return r.get('results') or {}


# ── helpers ───────────────────────────────────────────────────────────────

def paint(index, strokes, layer_name=None):
    return cmd('paint_stroke', {'image_index': index, 'layer_name': layer_name, 'strokes': strokes})


def sample(index, x, y, layer_name=None, merged=True):
    return results_of(cmd('sample_color', {'image_index': index, 'x': x, 'y': y,
                                           'layer_name': layer_name, 'sample_merged': merged}),
                      f'sample_color({x}, {y})')


def red_level(index, x, y):
    """Red channel 0-255 of the merged image; for black strokes on white, lower is darker."""
    return int(sample(index, x, y)['color_hex'][1:3], 16)


def new_test_canvas():
    """Create a white canvas and return its image_index."""
    r = cmd('new_canvas', {'width': WIDTH, 'height': HEIGHT, 'fill': 'white', 'name': 'paint_stroke_test'})
    image_id = r.get('image_id') or (r.get('results') or {}).get('image_id')
    if r.get('status') != 'success' or image_id is None:
        fail(f"new_canvas: {r.get('error', r)}")
    images = results_of(cmd('list_images', {}), 'list_images').get('images') or []
    for i, info in enumerate(images):
        if isinstance(info, dict) and info.get('image_id') == image_id:
            return i
    fail(f"new canvas (image_id={image_id}) not in list_images")


def close_image_if_open(index):
    """Close the test image so a persistent GIMP session stays clean."""
    if index is None:
        return
    try:
        r = cmd('close_image', {'image_index': index, 'save_first': False})
        if r.get('status') != 'success':
            print(f"  (cleanup) close_image non-success: {str(r.get('error') or '')[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"  (cleanup) close_image transport error: {e}", file=sys.stderr)


# ── checks ────────────────────────────────────────────────────────────────

def check_list_brushes():
    res = results_of(cmd('list_brushes', {}), 'list_brushes')
    if '2. Hardness 050' not in (res.get('brushes') or []):
        fail(f"list_brushes is missing the default brush: {res}")
    print(f"PASS list_brushes: {res.get('count')} brushes")


def check_exact_color(index):
    """A hex color lands exactly, and nothing is painted away from the stroke."""
    line = [[x, 40] for x in range(40, 361, 40)]
    res = results_of(paint(index, [{'points': line, 'brush': '2. Hardness 100', 'size': 24,
                                    'color': '#8b0000', 'pressure': 'none'}]), 'paint exact color')
    on, off = sample(index, 200, 40)['color_hex'], sample(index, 200, 90)['color_hex']
    if on != '#8b0000':
        fail(f"stroke color is {on}, expected #8b0000")
    if off != '#ffffff':
        fail(f"pixel away from the stroke is {off}, expected #ffffff")
    bbox = res.get('bbox') or {}
    if not (bbox.get('x', 999) <= 28 and bbox.get('x', 0) + bbox.get('width', 0) >= 372):
        fail(f"bbox {bbox} does not cover the stroke")
    print(f"PASS exact color: {on}, bbox {bbox}")


def half_width(index, x, y, max_offset=24):
    """Count dark pixels going down from (x, y)."""
    return sum(1 for dy in range(max_offset) if red_level(index, x, y + dy) < 128)


def check_taper(index):
    """pressure 'taper' makes the stroke wider in the middle than near its start."""
    results_of(paint(index, [{'points': [[40, 130], [200, 130], [360, 130]], 'brush': '2. Hardness 100',
                              'size': 40, 'color': '#000000'}]), 'paint taper')
    mid, end = half_width(index, 200, 130), half_width(index, 50, 130)
    if not (mid >= 5 and mid > end + 3):
        fail(f"no taper: half-width {mid}px in the middle vs {end}px near the start")
    print(f"PASS taper: half-width {mid}px in the middle, {end}px near the start")


def check_eraser(index):
    """The eraser clears alpha on a transparent layer."""
    results_of(cmd('create_layer', {'image_index': index, 'name': 'ink'}), 'create_layer ink')
    results_of(paint(index, [{'points': [[40, 210], [360, 210]], 'brush': '2. Hardness 100', 'size': 30,
                              'color': '#000000', 'pressure': 'none'}], 'ink'), 'paint ink')
    results_of(paint(index, [{'points': [[200, 180], [200, 240]], 'tool': 'eraser', 'brush': '2. Hardness 100',
                              'size': 30, 'pressure': 'none'}], 'ink'), 'erase ink')
    erased = sample(index, 200, 210, 'ink', merged=False)['alpha']
    kept = sample(index, 100, 210, 'ink', merged=False)['alpha']
    if erased > 0.05 or kept < 0.95:
        fail(f"eraser: alpha {erased} where erased, {kept} where kept")
    print(f"PASS eraser: alpha {erased} erased, {kept} kept")


def check_even_opacity(index):
    """A pressure list at 50% opacity has no darker beads where segments join."""
    results_of(cmd('create_layer', {'image_index': index, 'name': 'glaze'}), 'create_layer glaze')
    line = [[x, 280] for x in range(40, 361, 10)]
    res = results_of(paint(index, [{'points': line, 'brush': '2. Hardness 100', 'size': 20, 'color': '#000000',
                                    'opacity': 50, 'pressure': [1, 1], 'smooth': False}], 'glaze'),
                     'paint glaze')
    if res.get('layer') != 'glaze':
        fail(f"merged layer is named {res.get('layer')!r}, expected 'glaze'")
    levels = [red_level(index, x, 280) for x in (100, 105, 150, 155, 200, 205)]
    # 50% black lands near 128 or 188 depending on whether GIMP blends in
    # perceptual or linear light; only require it to be clearly partial.
    if max(levels) - min(levels) > 6 or not 60 <= sum(levels) / len(levels) <= 230:
        fail(f"uneven 50% stroke (joins at 100/150/200, midpoints at 105/155/205): {levels}")
    print(f"PASS even opacity: red levels {levels}")


def check_rejects_bad_batch(index):
    """Invalid strokes fail the whole batch before anything is painted."""
    good = {'points': [[40, 300], [360, 300]], 'color': '#00ff00', 'size': 10, 'pressure': 'none'}
    r = paint(index, [good, dict(good, color='orange')])
    if r.get('status') == 'success' or 'color' not in str(r.get('error')):
        fail(f"a named color should be rejected: {r}")
    if sample(index, 200, 300)['color_hex'] != '#ffffff':
        fail("the valid stroke was painted even though the batch was rejected")
    for bad, word in ((dict(good, brush='No Such Brush'), 'brush'), (dict(good, colour='#000000'), 'unknown keys'),
                      (dict(good, tool='smudge', pressure=[0, 1]), 'pressure list')):
        r = paint(index, [bad])
        if r.get('status') == 'success' or word not in str(r.get('error')):
            fail(f"expected an error mentioning {word!r}: {r}")
    print("PASS rejects bad batches without painting")


# ── entry point ───────────────────────────────────────────────────────────

def main():
    index = None
    try:
        check_list_brushes()
        index = new_test_canvas()
        check_exact_color(index)
        check_taper(index)
        check_eraser(index)
        check_even_opacity(index)
        check_rejects_bad_batch(index)
        print("PASS paint_stroke: all checks")
        sys.exit(0)
    finally:
        close_image_if_open(index)


if __name__ == '__main__':
    main()
