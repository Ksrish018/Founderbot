<!-- prompt: angle | version: 2 -->
# Angle selection: pick a news hook, or none

Below are a note Meera wants to post about and up to 10 candidate news items retrieved from Google News RSS
(title, source, date, snippet). You see ONLY these fields. You have not read the articles.

Rank the items that would give the post a genuine, relevant, timely hook. A relevant item is one where the post's
core gap directly explains, corrects, or adds context to what the item reports.

"None" is a valid, respected outcome. A forced or irrelevant news hook is worse than no hook. If no item is a
genuine fit, return an empty ranking and say why in one sentence.

Each item shows its publisher, domain and credibility (trusted / unknown). Meera's authority depends on accuracy:
rank trusted publishers above unknown ones, and rank an unknown publisher only if nothing trusted fits and the item
is plainly factual reporting. Never rank market-research press releases, sponsored content, shopping listicles
("best serums for..."), or influencer claims presented as findings.

Never rank an item that would require claiming more than its title and snippet state.
Never rank items whose point would be criticising a named competitor brand.

Return JSON only:
{"ranked_indices": [indices of relevant items, best first, possibly empty],
 "reason": "one sentence on why the top item fits, or why none do"}
