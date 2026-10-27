# OSM member export

Downloads the young people from every OSM section you can see into one spreadsheet (CSV, opens in Excel).

## One-off setup

1. In OSM go to **Settings → My Account Details → Developer Tools** and create a new application.
   - Name: anything, e.g. `ScoutTools export`
   - Redirect URL (if asked): `http://localhost:8765/callback`
2. Run `python osm_export.py discover` once to create `osm_config.json`, then copy the
   **client ID** and **client secret** into it.
   Treat the secret like a password: it gives access to members' details.
   `osm_config.json` is in `.gitignore`, so it is never uploaded to GitHub.
3. Run:

   ```
   python osm_export.py discover
   ```

   This lists your sections and every field OSM holds (labels only, no personal data).
   They are also saved to `fields_available.txt`.

## Each time you want a fresh list

```
python osm_export.py export
```

The file appears in `exports/members_<date>.csv`, one row per young person with a **Section** column.

## Choosing the columns

Edit the `fields` list in `osm_config.json`, using names exactly as shown by `discover`.
`*` means "everything in this group", e.g. `"Primary Contact 1 > *"`.

Other settings:
- `include_leaders`: `true` to include the adult leaders in each section.
- `skip_section_types`: section types to leave out (by default the Leaders and Waiting List sections).

## Looking after the data

The export contains children's and parents' personal details. Keep it on a secure device,
don't email it around, and delete old exports when you've finished with them.
