# Resources — Paper Set

The curated third-party sources backing this repo's design decisions.
Empty at repo birth: a row is added when a `web_search` lead proves
load-bearing, never speculatively. IDs must resolve on arXiv / OpenReview —
they are verified by being fetched, not by anyone's say-so.

Run [fetch_papers.py](fetch_papers.py) to download every PDF into `resources/pdf/`,
then `python resources/search.py --update` to index the new chunks.

| Key | Title | Source |
| --- | --- | --- |
