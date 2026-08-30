# Arxiv Papers Daily

A focused daily archive of computer vision papers from arXiv.

The site also includes **Milestone Models**, a curated view of official model
families. Its catalog defines release events and sources, and structured model
readings supply the comparison table. The first complete family is FLUX; the
other initial families are listed as planned.

[Arxiv Papers Daily](https://zyf515730395.github.io/arxiv-papers-daily/)

## Topics

- Image Generation
- Video Generation
- 3D Generation
- Neural Rendering
- Depth Estimation

## Project structure

```text
config/                         Site and Milestone Models catalogs
data/                           Collected paper metadata
docs/                           Generated GitHub Pages site
src/arxiv_papers_daily/papers/  Paper collection and archive theme
src/arxiv_papers_daily/milestones/  Milestone Models theme
```

New top-level themes should be added as sibling packages under
`src/arxiv_papers_daily/` so their catalog, renderer, and workflow code remain
isolated.
