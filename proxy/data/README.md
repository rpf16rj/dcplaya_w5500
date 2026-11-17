Put your lists here (or edit them in the `/admin` page):

- `channels.m3u`: your IPTV playlist (`#EXTINF:-1 ...,Name` + stream URL). It
  holds your IPTV account, so it never leaves the server and git ignores it.
- `youtube.m3u` (optional): YouTube channels, one per entry, e.g.
  ```
  #EXTM3U
  #EXTINF:-1,Some channel
  https://www.youtube.com/@somechannel
  ```

`/admin` keeps the previous versions in `backups/`.
