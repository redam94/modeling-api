---
template: home.html
title: Documentation
description: Fit, check and share models of labelled data with modeling-api. Find your path by role, do a task, or follow the Guided tutorial from a spreadsheet to a report.
hide:
  - footer
---

<section class="mono-band mono-band--hero mono-hero--home" markdown>
<div class="mono-wrap mono-center" markdown>

<p class="mono-kicker">modeling-api documentation</p>

# Trustworthy models, from spreadsheet to endpoint. { .mono-display }

You bring the data and the question. The platform fits the model, checks it and
**tells you how sure each answer is**.
{ .mono-lead }

<div class="mono-actions" markdown>
[Start the Guided tutorial](guides/guided-tutorial.md){ .md-button .md-button--primary }
[Install modeling-api](getting-started/index.md#install){ .mono-more }
</div>

</div>
</section>

<section class="mono-band mono-band--alt mono-band--tight tex-contours" markdown>
<div class="mono-wrap mono-center" markdown>

## Choose your path. { .mono-headline data-reveal }

Four roles use the platform. **Each role has a short path.**
{ .mono-lead data-reveal }

<div class="mono-tiles mono-tiles--routes" markdown>

-   ![](assets/brand/icons/analyst.webp){ .mono-icon }

    ### Analyst

    Upload a spreadsheet. Get a report, a scorecard and what-if sliders, with no code.

    [Do the Guided tutorial](guides/guided-tutorial.md){ .mono-more }

    [Take the tour of the web app](guides/start-here.md){ .mono-more }

    [Build a client dashboard](guides/client-dashboards.md){ .mono-more }

-   ![](assets/brand/icons/developer.webp){ .mono-icon }

    ### Developer

    Write a spec or some lines of Python. Add your own models, steps and plots.

    [Read the quickstart](getting-started/quickstart.md){ .mono-more }

    [Extend the platform](extend/index.md){ .mono-more }

    [Look up the spec format](reference/spec-format.md){ .mono-more }

-   ![](assets/brand/icons/operator.webp){ .mono-icon }

    ### Operator

    Install the platform, keep it secure and manage the teams that use it.

    [Read the deployment overview](operations/index.md){ .mono-more }

    [Start with Docker Compose](operations/docker.md){ .mono-more }

    [Manage members and quotas](guides/administration.md){ .mono-more }

-   ![](assets/brand/icons/client.webp){ .mono-icon }

    ### Client

    An analyst made a dashboard for you. Read it and ask questions about it.

    [Read your dashboard](guides/for-clients.md){ .mono-more }

    [Ask about your dashboard](guides/ask-about-your-dashboard.md){ .mono-more }

</div>

</div>
</section>

<section class="mono-band mono-band--tight" markdown>
<div class="mono-wrap" markdown>

<p class="mono-kicker">Quick links</p>

## Do a task. { .mono-headline .mono-headline--s }

<div class="mono-tiles mono-tiles--4 mono-tiles--compact" markdown>

-   ![](assets/brand/icons/install.webp){ .mono-icon }

    ### Install

    [Install the packages](getting-started/index.md#install){ .mono-more }

-   ![](assets/brand/icons/sign-in.webp){ .mono-icon }

    ### Sign in

    [Sign in with a browser](guides/authentication.md#browser-sign-in){ .mono-more }

-   ![](assets/brand/icons/upload-data.webp){ .mono-icon }

    ### Upload data

    [Upload a spreadsheet](guides/guided-data.md){ .mono-more }

-   ![](assets/brand/icons/choose-model.webp){ .mono-icon }

    ### Choose a model

    [Get a ranked shortlist](guides/choosing-a-model.md){ .mono-more }

-   ![](assets/brand/icons/null-tests.webp){ .mono-icon }

    ### Check a model

    [Run the null tests](guides/null-tests.md){ .mono-more }

-   ![](assets/brand/icons/compare-runs.webp){ .mono-icon }

    ### Compare runs

    [Compare runs side by side](guides/run-comparison.md){ .mono-more }

-   ![](assets/brand/icons/dashboard.webp){ .mono-icon }

    ### Build a dashboard

    [Make a client dashboard](guides/client-dashboards.md){ .mono-more }

-   ![](assets/brand/icons/share-link.webp){ .mono-icon }

    ### Share a link

    [Make a share link](guides/sharing-dashboards-securely.md#make-a-link){ .mono-more }

-   ![](assets/brand/icons/upload-code.webp){ .mono-icon }

    ### Upload code

    [Upload your own model](extend/upload.md){ .mono-more }

-   ![](assets/brand/icons/workflow.webp){ .mono-icon }

    ### Automate

    [Copy a workflow recipe](guides/workflow-cookbook.md){ .mono-more }

-   ![](assets/brand/icons/error-codes.webp){ .mono-icon }

    ### Error codes

    [Find an error code](reference/generated/error-codes.md){ .mono-more }

-   ![](assets/brand/icons/troubleshoot.webp){ .mono-icon }

    ### Troubleshoot

    [Fix a problem](operations/troubleshooting.md){ .mono-more }

</div>

</div>
</section>

<section class="mono-band mono-band--alt mono-band--tight" markdown>
<div class="mono-wrap" markdown>

<p class="mono-kicker">Product tour</p>

## Your first MMM, in four steps. { .mono-headline .mono-headline--s data-reveal }

<div class="mono-tour" markdown>

<div markdown>

<div class="mono-steps" markdown>

1.  ### Choose a template and upload your file

    The platform suggests which column is revenue and which columns are spend. It tells you why.

2.  ### Fit the model

    The progress shows in plain language. The platform tests the model on weeks that it did not see.

3.  ### Read the results

    You see the return of each channel with its range. A **sanity scorecard** tells you if you can trust it.

4.  ### Ask "what if"

    Move a slider and see the change in revenue immediately. Then share the result with a link.

</div>

<div class="mono-actions" markdown>
[Do the full tutorial](guides/guided-tutorial.md){ .mono-more }
</div>

</div>

<div class="mapi-shots" markdown>

<figure markdown>
![The report with the return and the share of each channel](assets/screenshots/guided-report.png){ .mapi-shot loading=lazy }
<figcaption>Step 3: each number has the range that it can be in.</figcaption>
</figure>

<figure markdown>
![The what-if sliders with the exact run](assets/screenshots/guided-whatif.png){ .mapi-shot loading=lazy }
<figcaption>Step 4: the sliders answer immediately.</figcaption>
</figure>

</div>

</div>

</div>
</section>

<section class="mono-band mono-band--dark mono-band--hero mono-band--left mono-band--tight mono-hero--reference" markdown>
<div class="mono-wrap" markdown>

## What is in the box. { .mono-headline data-reveal }

<div class="mono-figures" markdown>
<div data-reveal markdown>
<p class="mono-figure-num">51</p>

Built-in models, in the [models catalog](reference/generated/models-catalog.md).
</div>
<div data-reveal markdown>
<p class="mono-figure-num is-orange">57</p>

Plot recipes, in the [plot recipes catalog](reference/generated/plots-catalog.md).
</div>
<div data-reveal markdown>
<p class="mono-figure-num is-blue">57</p>

Workflow nodes, in the [workflow node reference](reference/workflow-nodes.md).
</div>
<div data-reveal markdown>
<p class="mono-figure-num">29</p>

Preprocessing steps, in the [steps catalog](reference/generated/steps-catalog.md).
</div>
</div>

</div>
</section>

<section class="mono-band mono-band--tight" markdown>
<div class="mono-wrap mono-center" markdown>

<p class="mono-kicker">What's new</p>

## One look, one search. { .mono-headline .mono-headline--s }

Version 0.21 gives the app and the docs one calm look. **Search finds your pages, your work and the docs.**
{ .mono-lead }

<div class="mono-actions" markdown>
[Read what is new in 0.21](whats-new-0.21.md){ .md-button .md-button--primary }
[See the roadmap](roadmap/index.md){ .mono-more }
</div>

</div>
</section>

<section class="mono-band mono-band--alt mono-band--tight" markdown>
<div class="mono-wrap mono-center" markdown>

The documentation is CC BY 4.0. The SDK is Apache-2.0. The platform is AGPLv3 or commercial.
Read the [licensing page](project/licensing.md).
{ .mono-lead }

<div class="mono-actions" markdown>
[Why modeling-api?](why.md){ .mono-more }
[See the use cases](use-cases.md){ .mono-more }
</div>

</div>
</section>
