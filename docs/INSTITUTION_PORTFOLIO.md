# Portfolio (batch) analysis

For anyone who must review many projects: a university marking submissions, a company checking vendor deliveries,
a government body assessing contractors.

## Use
- GUI: **Portfolio** tab -> choose a folder of ZIPs -> pick a profile -> **Analyze folder**. Double-click a row to open that
  project's HTML report. **Open portfolio report** / **Open CSV** open the consolidated files.
- CLI: `python app.py --batch D:\submissions --profile education --recursive --out D:\review`
  Exit code: 0 = no project failed, 1 = at least one FAIL, 2 = bad folder/profile or no ZIPs found. Useful in scheduled jobs.

## Output (in the output folder)
- `portfolio_<time>.html`, `.csv` (Excel-friendly, formula-injection safe), `.json`
- `projects/<name>/...html|json`: the normal report of every project, linked from the portfolio page.

## Profiles
`config/institution_profiles.json` (optional file; built-in defaults are used when it is missing or damaged). Add your own:
`{"profiles": {"my-college": {"max_high": 3, "max_secrets": 0, "similarity_review_at": 0.75}}}`
Keys: `max_critical`, `max_high`, `max_secrets`, `max_syntax_errors` (whole numbers, `null` = not checked),
`similarity_review_at` (0-1, `null` = off). Invalid values are ignored with a warning.

## How to read the results
- **FAIL**: a threshold is exceeded, or the archive was rejected by safety validation. **REVIEW**: within thresholds but
  shares content with another project at or above the profile's similarity level. **PASS**: within thresholds.
- Verdicts are advice for a human reviewer. Findings are static-analysis heuristics unless marked verified.
- Risk = 25 x critical + 10 x high + 3 x medium + 1 x low, capped at 100. It orders review work; it is not a grade.
- Similarity = shared (CRC32, size) pairs of non-vendored files >= 64 bytes divided by all distinct pairs. It detects copied
  files, not renamed-and-edited code. Both parties of a pair are flagged because the tool cannot tell who copied whom.

## Safety
Archives are read in place, never extracted, never executed, never modified. One corrupt archive does not stop the batch.
