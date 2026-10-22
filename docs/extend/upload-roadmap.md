---
description: What comes in modeling-api 0.19 - upload a Python plugin to a shared deployment, checked in the sandbox and approved by a second person.
---

# Coming in 0.19: upload your plugin <span class="mapi-badge mapi-badge--soon">planned</span>

In v0.18, Python plugins run only on a stack that you operate
([Run your plugin on your stack](develop/run-on-your-stack.md)). Version 0.19 plans to let you
upload a plugin to a shared deployment, with these controls. The details can change before
the release.

- **Who may upload.** Tenant admins and users whose identity is verified. The public tier
  stays blocked.
- **A second person approves.** An uploaded plugin is usable in its own project first.
  Before every project of the tenant can use it, another person approves it. Nobody approves
  their own upload.
- **The same checks, in the sandbox.** The platform unpacks, builds and describes the plugin
  in the runner sandbox, then runs the contract kits that you run today
  ([`check_model`, `check_step`, `check_recipe`, `check_node`, `check_template`](develop/contract-kits.md))
  with complete About cards required. Settings schemas are refused, not stripped, when they
  are not safe.
- **Your own namespace.** Uploaded ids live under a namespace of the tenant (`t_<tenant>_...`),
  so they never shadow a built-in or another tenant's plugin. v0.18 already keeps every
  namespace that starts with `t_` or `tenant_` free for this.
- **No surprises in the build.** Pure Python packages only, with dependencies from a curated
  mirror that is pinned to the base image. No runtime `pip`, no network.
- **Visible problems.** What fails shows in the same places as today: `mapi plugin doctor`
  and Catalog › Yours › Problems.

## Get ready now

What you build for v0.18 carries over:

1. Scaffold with `mapi plugin init` and keep `mapi plugin test` green.
2. Fill in every About card. Uploads will need them.
3. Keep your settings schemas inside the
   [schema-safety limits](../reference/generated/extension-points.md#settings-and-parameter-schemas).
4. Use only the platform's packages, or vendor what you need as pure Python.

See also the [roadmap](../roadmap/expert-plugins.md).
