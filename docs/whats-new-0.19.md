---
description: v0.19 lets you upload your own Python - models, prep steps, plot recipes, workflow nodes and templates - checked in the sandbox, private to you first, approved for your team by a second admin.
---

# What's new in 0.19 <span class="mapi-badge">v0.19</span>

**Upload your own code.** In v0.18 a Python plugin ran only on a stack that you operate. Now
you can push it to a shared deployment. The platform checks the file and runs its tests in
the sandbox. You can use it yourself straight away, and a second admin approves it for the
whole team.

<div class="grid cards" markdown>

-   :material-cloud-upload-outline:{ .lg .middle } **Push a wheel**

    ---

    `mapi plugin init acme --upload` claims your namespace (`t_<code>_acme`) and scaffolds
    the package. `mapi plugin push --wait` builds the wheel, uploads it and waits for the
    checks. Or drop the wheel on the upload page in the app.

    [:octicons-arrow-right-24: Upload your own code](extend/upload.md)

-   :material-shield-check-outline:{ .lg .middle } **Checked without running it**

    ---

    The first check reads the wheel as an archive and runs none of it: one pure-Python
    package, no `.pth` files, no native code, no zip bombs, only packages the platform
    already has. You get an SBOM of every file.

    [:octicons-arrow-right-24: The rules for a wheel](extend/upload.md#the-rules-for-a-wheel)

-   :material-test-tube:{ .lg .middle } **Tested in the sandbox**

    ---

    The second check loads your package in a sandbox run and does the contract kits that
    `mapi plugin test` does, with small example data where it fits.

    [:octicons-arrow-right-24: Contract kits](extend/develop/contract-kits.md)

-   :material-account-multiple-check-outline:{ .lg .middle } **A second person approves**

    ---

    Your version is "Ready for you" first: only you can use it. A different tenant admin,
    freshly signed in, reviews the facts, the SBOM, the links and the test report, and
    approves exactly that file for the team.

    [:octicons-arrow-right-24: Review uploaded code](extend/review-uploads.md)

-   :material-lock-outline:{ .lg .middle } **Kept inside your workspace**

    ---

    Uploads never reach share links, client portals or embeds, and never rank in Help me
    choose. Each run that uses uploaded code is marked, pinned to the exact file and gets an
    "Uploaded code" badge.

-   :material-server-security:{ .lg .middle } **Operators stay in control**

    ---

    Uploaded code runs only on Docker, gVisor or Cloud Run. The kill switch pauses it, a
    withdrawal with the reason `security` cancels its runs, and refused files are purged
    after 30 days.

    [:octicons-arrow-right-24: Uploaded code for operators](operations/uploaded-code.md)

</div>

## For developers

- New CLI commands: `mapi plugin init NAME --upload`, `mapi plugin push`,
  `mapi plugin status` and `mapi plugin yank`. A token with the `plugins:publish` scope can
  push and ask for approval. It can never approve or withdraw.
- New routes under `/v1/plugins` and `/v1/tenants/{tenant_id}/plugin-settings`; the
  [extension points reference](reference/generated/extension-points.md#uploaded-plugins)
  lists them with their error codes.
- Two new run kinds, `plugin_inspect` and `plugin_describe`. Lineage gets `overlays`: the
  uploaded versions a model version used.
- Six webhook events: `plugin.version.kit_passed`, `kit_failed`, `awaiting_approval`,
  `approved`, `rejected` and `yanked`.
- `make plugin-push PLUGIN=../acme` and `make security-uploads`.

## Good to know

- Wheels only, at most 20 MiB, pure Python, and only packages that the platform image
  already has. There is no package mirror.
- A workspace with one admin, and a single-tenant deployment, can use uploads privately only:
  nobody is there to approve.
- Isolation on gVisor and Cloud Run for uploaded code is not verified yet. Hosted
  deployments must do the [cloud gates](operations/uploaded-code.md#cloud-gates) before
  verified workspaces upload.
