# site/ — judge-facing web surfaces

Two self-contained HTML files. No build step, no dependencies, no server required.

| File | What it is | Open it |
|---|---|---|
| `index.html` | Landing page — the 60-second case: the operator, the loop, the verified proof, the reproduce commands | double-click, or serve the folder |
| `pitch-deck.html` | 10-slide pitch deck — arrow/space to navigate, `ESC` overview grid, `P` presenter notes, `C` contrast check, print-to-PDF at 16×9 landscape | double-click |

Both open directly from `file://`.

## Serving locally

```bash
python3 -m http.server 8000 --directory site
# -> http://localhost:8000
```

Every figure on both pages is produced by a script in this repository — see
[`../DEMO.md`](../DEMO.md) for the command behind each one, and
[`../README.md`](../README.md#️-what-is-real-and-what-is-not) for what is real and
what is synthetic.
