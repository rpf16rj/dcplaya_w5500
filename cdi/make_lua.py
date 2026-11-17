"""Generate the patched Lua files from the originals on the disc.

  NET.LUA           <- NET.LUA.orig + DHCP (net_autoconnect.lua) + loads netprobe.lez
  SONG_BROWSER.LUA  <- SONG_BROWSER.LUA.orig + asynchronous check of /http/ streams
  KEYBOARD_EMU.LUA  <- KEYBOARD_EMU.LUA.orig + Start pauses/resumes playback
  PLAYLIST.LUA      <- PLAYLIST.LUA.orig + #DCPLAYA-REMOTE: playlists loaded from the proxy

usage: python make_lua.py   (expects NET.LUA.orig and SONG_BROWSER.LUA.orig here;
                             extract them with: python extract_file.py)
"""


def sub1(s, old, new):
    assert s.count(old) == 1, ('anchor not unique/found', old)
    return s.replace(old, new)


def make_net():
    s = open('NET.LUA.orig', 'rb').read().decode('latin-1')
    s = sub1(s, 'name="192.168.1.50"', 'name="dhcp"')
    a = s.index('function net_autoconnect()')
    b = s.rindex('net_connect_dialog()')
    s = s[:a] + open('net_autoconnect.lua', encoding='utf-8').read() + s[b:]
    open('NET.LUA', 'wb').write(s.encode('latin-1'))


PROBE_WAIT = '''
--- W5500 patch: wait for the background check of an http stream
--- (net_probe, from plugins/exe/net/netprobe.lez) without blocking the UI.
--- @return 1 while the playlist must not advance this frame
--- @internal
function song_browser_probe_wait(sb, frametime)
   local p = sb.probe
   local st, msg = net_probe_status()
   p.elapsed = p.elapsed + (frametime or 0)
   if not st and p.elapsed < (sb.probe_timeout or 12) then
      return 1
   end
   sb.probe = nil
   if st == 1 then
      -- server answered: select the same entry again, it will be played
      sb.probe_ok = p.path
      sb.playlist_idx = p.prev
   else
      net_probe_cancel()
      print("playlist: skipping unreachable stream "..p.path.." : "
	    ..(msg or "timeout"))
      sb.playlist_first_failure = sb.playlist_first_failure or p.idx
   end
   return 1
end

--- Update the playlist running.'''


def make_song_browser():
    s = open('SONG_BROWSER.LUA.orig', 'rb').read().decode('latin-1')

    s = sub1(s, '\n--- Update the playlist running.', PROBE_WAIT)

    # stop / restart forget any running probe
    s = sub1(s, '      sb.playlist_idx = nil\n   elseif',
             '      sb.playlist_idx = nil\n      sb.probe = nil\n   elseif')
    s = sub1(s, '      sb.playlist_first_failure = nil\n   end\n',
             '      sb.playlist_first_failure = nil\n      sb.probe = nil\n'
             '      sb.probe_ok = nil\n   end\n')

    s = sub1(s, '   if not sb.playlist_idx then\n      return\n   end\n',
             '   if not sb.playlist_idx then\n      return\n   end\n\n'
             '   -- W5500 patch: an http stream is being checked in background\n'
             '   if sb.probe and song_browser_probe_wait(sb, frametime) then\n'
             '      return\n   end\n')

    s = sub1(s, '      sb.playlist_idx = nxt\n',
             '      local prev_idx = sb.playlist_idx\n      sb.playlist_idx = nxt\n')

    s = sub1(s, ' \t print(path)\n',
             '\t -- W5500 patch: check http streams in background first, so a\n'
             '\t -- dead link does not freeze the interface in playa_play().\n'
             '\t if path and net_probe and strsub(path, 1, 6) == "/http/"\n'
             '\t    and sb.probe_ok ~= path and net_probe(path) then\n'
             '\t    sb.probe = { path = path, idx = sb.playlist_idx,\n'
             '\t\t\t prev = prev_idx, elapsed = 0 }\n'
             '\t    sb.pl:draw()\n'
             '\t    return\n'
             '\t end\n'
             '\t sb.probe_ok = nil\n'
             ' \t print(path)\n')

    s = sub1(s, '   sb.playlist_idx = nil\n   sb.playlist_start_pos = nil\n',
             '   sb.playlist_idx = nil\n   sb.playlist_start_pos = nil\n'
             '   sb.probe = nil\n   sb.probe_ok = nil\n'
             '   if net_probe_cancel then net_probe_cancel() end\n')

    # "B" (cancel) must also stop a playlist that is waiting on a probe
    s = sub1(s, '   if playa_play() == 1 then\n      sb:stop();',
             '   if playa_play() == 1 or sb.probe then\n      sb:stop();')

    # "B" in the playlist: stop while something plays (or the playlist runs);
    # remove the entry only when stopped (also in the X menu: remove).
    s = sub1(s, 'function sbpl_cancel(pl, sb)\n   sbpl_remove(sb)\n',
             'function sbpl_cancel(pl, sb)\n'
             '   if playa_play() == 1 or sb.probe or sb.playlist_idx then\n'
             '      sb:stop()\n'
             '      return\n'
             '   end\n'
             '   sbpl_remove(sb)\n')

    open('SONG_BROWSER.LUA', 'wb').write(s.encode('latin-1'))


PAUSE_KEY = '''   -- W5500 patch: while something is playing and the virtual keyboard
   -- is closed, Start pauses/resumes (playa_pause, alias "pause").
   if ke_keyactivate[key] and not ke_shadow_active and pause
      and playa_play() == 1 then
      if pause() == 0 then pause(1) else pause(0) end
      return nil
   end

   -- activating key toggle'''


REMOTE = '''   local file = openfile(fname,"r")
   if not file then
      print("playlist_load : open error ["..fname.."]")
      return
   end

   -- W5500 patch: "#DCPLAYA-REMOTE:/http/..." lines at the top of a playlist
   -- point to an always up-to-date copy (e.g. YouTube channel lists on the TV
   -- proxy).  The first one that answers with an m3u is used; otherwise the
   -- rest of this file is the fallback.
   local line = read(file)
   while line do
      local s, e, remote = strfind(line, "^#DCPLAYA%-REMOTE:([^\\r]+)")
      if not s then break end
      print("playlist : trying "..remote)
      local rfile = openfile(remote, "r")
      if rfile then
         local head = read(rfile)
         if head and strfind(head, "^#EXTM3U") then
            print("playlist : using "..remote)
            closefile(file)
            file = rfile
            line = nil
            break
         end
         closefile(rfile)
      end
      line = read(file)
   end
   if line then
      seek(file, "set", 0)
   end
'''


def make_playlist():
    s = open('PLAYLIST.LUA.orig', 'rb').read().decode('latin-1')
    s = sub1(s, '''   local file = openfile(fname,"r")
   if not file then
      print("playlist_load : open error ["..fname.."]")
      return
   end
''', REMOTE)
    open('PLAYLIST.LUA', 'wb').write(s.encode('latin-1'))


def make_keyboard_emu():
    s = open('KEYBOARD_EMU.LUA.orig', 'rb').read().decode('latin-1')
    s = sub1(s, '   -- activating key toggle', PAUSE_KEY)
    open('KEYBOARD_EMU.LUA', 'wb').write(s.encode('latin-1'))


if __name__ == '__main__':
    make_net()
    make_song_browser()
    make_keyboard_emu()
    make_playlist()
    print('NET.LUA, SONG_BROWSER.LUA, KEYBOARD_EMU.LUA and PLAYLIST.LUA generated')
