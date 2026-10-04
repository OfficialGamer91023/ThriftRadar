---
title: ThriftRadar
emoji: 👟
colorFrom: gray
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
short_description: Search shoes posted in a WhatsApp thrift group (demo)
---

# ThriftRadar demo

ThriftRadar turns a WhatsApp thrift group's shoe posts into a searchable catalogue: it finds the shoe in each
photo, reads the caption and size tag, fills gaps with a vision-language model, spots reposts, and matches new
posts against wishlists.

This Space is a demo. Log in with the credentials printed on the login page, search the sample listings, or
simulate a post of your own (visible only to you, deleted after 24 hours).

- **No real group data.** The sample listings use licensed photos (see the Credits page) with made-up sizes and
  prices. The demo database is built from them when the image is built and can't hold anything else.
- **Limits.** Uploads, searches and AI calls are rate-limited and capped per day.
