# Web fidelity (`hoard_link.webfidelity`)

Scores how close a rebuilt site is to its reference. Used by the website-rebuild bench of the evaluation studio and by the clones of the
web-design studio, so both give the same number for the same pair of sites.

## Cases

A case is a list of steps run in a browser on both sites. One key per step:

| Step | Does |
|---|---|
| `{"goto": "/route"}` | open a route of the site (`settle_ms` optional) |
| `{"click": "<selector>"}` | click the first match (Playwright selectors: CSS, `text=…`, `role=…`) |
| `{"fill": "<selector>", "text": "…"}` | type into a field |
| `{"press": "Enter", "on": "<selector>"}` | press a key |
| `{"select": "<selector>", "option": "…"}` | choose an option |
| `{"scroll": 600}` / `{"wait": 300}` | scroll to a y position / wait in ms |
| `{"expect_text": "…", "in": "<selector>"}` | the text is shown (case-insensitive); `expect_no_text` the opposite |
| `{"expect_url": "/route"}` | the page is at that route |
| `{"expect_count": "<selector>", "n": 3}` | that many matches |
| `{"expect_value": "<selector>", "value": "…"}` | a field holds that value |
| `{"checkpoint": "name"}` | a picture of the viewport, paired with the reference's by route, width and name |

`validate_case` checks a case; `run_case(page, base_url, case, width=…, shoot=…)` runs it on a Playwright page given by the caller and
returns the checks and the checkpoints (the file `shoot` wrote for each). A step that cannot run fails the case and stops it.
`FREEZE_JS` is an init script that turns off transitions and animations and fixes the clock and the random numbers.

## Scores

- `normalized_ssim(reference, candidate)`: RGB SSIM over 7x7 windows, rescaled against a flat picture of the reference's median colour:
  `(ssim - baseline) / (1 - baseline)` in [0, 1]. The candidate is cut or padded to the reference's size; missing rows count as different.
- `score_case(id, checks, checkpoints, threshold=0.99)`: passes when every check passed and every checkpoint reached the threshold.
- `score_site(cases)`: the share of cases that pass, the mean visual score and the share of checks that passed.
- `diff_regions(reference, candidate)`: the zones that differ most as boxes, worst first; `overlay(picture, regions)` outlines and
  numbers them on a copy of the picture.

Pictures are paths, PNG/JPEG bytes, Pillow images or arrays. numpy and Pillow are imported when a score is computed; the step runner needs
neither, so a browser worker can load the file by path.
