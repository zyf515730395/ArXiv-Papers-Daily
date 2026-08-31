---
title: Writings source bundles
slug: writings
published_at: 2026-08-31
kind: learning-note
public: true
summary: Contract for repository-owned public writing bundles.
tags: [writings]
source: original
---

# Writings source contract

- Each bundle directory name must equal the article `slug` and contain its `index.md`.
- Keep local images only below that bundle's `assets/` directory.
- Every repository article must set `public: true`.
- Removing a bundle unpublishes that article.
- Keep drafts and private identifiers outside this repository.

This directory-level contract file intentionally keeps the approved source root empty; it is not an article bundle.
