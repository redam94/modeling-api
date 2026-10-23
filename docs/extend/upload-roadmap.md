---
description: Moved - plugin upload shipped in modeling-api 0.19. See Upload your own code.
---

# Plugin upload has moved

Plugin upload is available from v0.19. This page described the plan. Read these pages:

- [Upload your own code](upload.md): push a wheel, use it yourself, ask for approval.
- [Review uploaded code](review-uploads.md): the guide for the tenant admins who approve.
- [Uploaded code for operators](../operations/uploaded-code.md): where uploaded code runs,
  the settings and the kill switch.

Two items changed from the plan: the API takes **wheels only** (`mapi plugin push` builds the
wheel on your machine), and there is **no package mirror**. Each requirement must already be
in the base image of the platform.
