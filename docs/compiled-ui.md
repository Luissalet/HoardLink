# Compiled interfaces

Open **System → Compiled interfaces** in the Hub, or call
`hub_compiled_ui_status` with an optional `app` ID. Both use the same
read-only check. The Hub compares the registered checkout with the response
from the registered installation. The check starts no application and runs no
build or restart.

A stale build shows the number of interface commits it lacks and the date of
the earliest one. Expand an application to see its output folder, SHA-256,
source commit and whether the installed response matched. The fingerprint
covers the local entry and its directly linked scripts and styles. The API
also returns the SHA-256 of the actual HTTP entry as `served_entry_sha256`,
when it could read it. A missing build, a stopped application and a mismatched
response have distinct results.

## How the Hub finds a build

- For the Hub itself, the console reads `apps/agora/dist/index.html` at
  `/agora/`. The inputs include the console sources and the shared production
  decision files it imports.
- Faustus declares `static/studio` in its Vite configuration. The Hub checks
  `/static/studio/studio.js` on its configured local service.
- Other registered applications can declare their output in the
  `x-family.compiled_ui` block of `faustus-plugin.json`. Use repository-relative
  paths for `output` and `sources`, an output-relative `entry`, and the served
  `route` and `asset_base`, including the leading slash. The defaults are
  `index.html`, `/` and `/`. Set `compiled_ui` to `false` for an app with no
  compiled interface. For example:

  ```json
  {
    "output": "product/static",
    "sources": ["client", "vite.config.js", "package.json", "package-lock.json"],
    "entry": "index.html",
    "route": "/",
    "asset_base": "/"
  }
  ```

- Otherwise the Hub reads literal Vite `root` and `build.outDir` from the
  project root or a `client`, `frontend`, `web`, `ui` or `apps/web` subdirectory. If Vite
  leaves them unset, its defaults are the project folder and `dist`. Vulcan's
  Hoard declares `root: 'client'` and `outDir: '../vulcan/static'`. Build roots
  from executable expressions require a manifest declaration. The Hub only
  checks build entries ignored by Git.
- The HTTP comparison uses the registered application URL or the address in
  the existing placement record. It does not follow redirects. If an app is
  only available as a native desktop bundle, the Hub can still report the
  files in its checkout without verifying the installed response.

## How the Hub compares revisions

The integration builder publishes `hoard-ui-build.json` beside the entry
for the console and Studio. It records the exact source commit, build date
and entry digest. The monitor uses Git ancestry and counts the path-filtered
commits between that revision and the installed checkout. For manual builds
without a receipt, it estimates the revision from the entry's modification
time and the checkout's first-parent history, then counts the missing source
commits. A copied or retimestamped output cannot establish its source
revision by date alone. The comparison basis is visible in the panel and API.

Ágora checks the Git paths of both the submitted commits and the proposed
closing commits. A UI delivery for a detected build can close when the
registered checkout contains the interface changes, its build covers the
tracked sources, and the running local service returns that build. A
verified integration can supply equivalent replacement commits. An old
build or an unverified response returns HTTP 409 with the reason in Spanish
and English. Rebuild in a maintenance window, reload the installed
application, verify the change in its window, and include the build date and
capture in the closure. Other deliveries use the existing closure flow.
