# Update the configuration and the README

Apply exactly these edits:

`app.conf` (INI style; keep every comment line and every other key exactly as it is and in the same order):
1. In `[server]`, set `port` to `8080`.
2. Remove the deprecated key `use_ssl2` (the whole line).
3. Append a new section at the end of the file:

       [logging]
       level = info
       file = /var/log/app.log

`README.md`:
4. Rename the heading `## Setup` to `## Installation`.
5. Replace every occurrence of `localhost:3000` with `localhost:8080`. Change nothing else (the sentence mentioning
   that the port "used to be 3000" stays as it is).
