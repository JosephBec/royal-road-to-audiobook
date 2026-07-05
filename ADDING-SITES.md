# Adding a New Site

Novel TTS can read from any website it has a "scraper" for. A scraper is one small
file that teaches the app how to find a novel's title, chapter list, and chapter
text on a particular website. The app ships with scrapers for Royal Road and
Ranobes, and it automatically picks up any new ones you add.

You do not need to be a programmer to add one. The easiest way is to let an AI
coding tool write it for you.

## The easy way: ask an AI to write it

Use any AI coding tool that can read and write files on your computer — for
example Claude Code, Cursor, or Codex. They all work the same way for this job:
you open the tool inside this project folder and tell it what you want.

1. **Open the tool in this project's folder** (the folder this file is in).

2. **Ask it to write the scraper.** Say something like:

   > Read `scrapers/base.py` and `scrapers/royalroad.py` to see how scrapers
   > work in this project. Then write a new scraper for this site:
   > `https://www.example-novel-site.com`. Put it in the `scrapers/` folder.
   > Here is a link to one novel on that site so you can check the page
   > layout: `https://www.example-novel-site.com/novel/12345/some-story`

   Giving it a real novel link helps a lot — the tool can look at the actual
   page and figure out where the title, chapters, and text live.

3. **Restart the server.** Close it and start it again the way you normally do.
   The new scraper is found automatically — nothing to register anywhere.

4. **Check it shows up.** Click "+ Add Novel" in the app, then "Check the
   supported sites". Your new site should be in the list.

5. **Test it.** Paste a real novel link from that site and add it. Then check:
   - The novel appears with the right title and cover.
   - The chapter list looks complete and in order.
   - Playing a chapter reads the story text — not menus, ads, or author notes.

6. **If something's wrong, tell the AI.** Copy whatever error you saw (or just
   describe what looked wrong, like "the chapters are all named Untitled") back
   into the tool and ask it to fix the scraper. One or two rounds of this is
   normal.

## Good to know

- **One file per site.** Each scraper lives in the `scrapers/` folder. Deleting
  the file removes support for that site. A broken scraper file won't crash the
  app — it just gets skipped.
- **Websites change.** If a site that used to work stops working, its page
  layout probably changed. The fix is the same routine: ask an AI tool to read
  the existing scraper for that site and repair it.
- **Be polite to the site.** Scrapers in this project fetch one page per second
  on purpose. Keep that behavior in anything new — it's built into the example
  scrapers the AI will copy from.
- **For the technically curious**, the exact requirements a scraper has to meet
  are described in `docs/DEVELOPMENT.md`.
