dhcp_req_filename = "/ram/dhcp.req"
dhcp_res_filename = "/ram/dhcp.lua"

-- IP field "dhcp" (or empty / 0.0.0.0) selects automatic configuration.
-- DHCP itself runs inside the bba.lez network driver (W5500 or BBA).
function net_is_dhcp(ip)
   return not ip or ip == "" or ip == "0.0.0.0" or strlower(ip) == "dhcp"
end

function net_file_exists(name)
   local f = openfile(name, "r")
   if f then
      closefile(f)
      return 1
   end
end

function net_autoconnect()
   if not net_cfg then
      return
   end

   if not net_connect then
      dl(plug_net)
   end

   if not net_connect then
      gui_ask("Could not load net driver !", {"OK"})
      return
   end

   local use_dhcp = net_is_dhcp(net_cfg.ip)
   local connect_ip = net_cfg.ip
   local ip, mask, gw, dns = net_cfg.ip, net_cfg.mask, net_cfg.gw, net_cfg.dns

   if use_dhcp then
      connect_ip = "0.0.0.0"
      dhcp_cfg = nil
      if net_file_exists(dhcp_res_filename) then
	 rm(dhcp_res_filename)
      end
      local f = openfile(dhcp_req_filename, "w")
      if f then
	 write(f, "1")
	 closefile(f)
      end
   end

   sc()
   local scroll = gui_scrolltext and
      gui_scrolltext(nil, use_dhcp and "Bringing network up (DHCP) ..."
		     or "Bringing network up ...")

   local res = net_connect(connect_ip)
   local text = "<left>"
   local ok = not res

   if use_dhcp then
      if net_file_exists(dhcp_req_filename) then
	 rm(dhcp_req_filename)
      end
      if net_file_exists(dhcp_res_filename) then
	 dofile(dhcp_res_filename)
      end
   end

   if not ok then
      text = text..'net_connect ... FAILED<br>Either you don\'t have any BBA, W5500 or Lan adaptor, either you are already connected ?'
   elseif use_dhcp and not (dhcp_cfg and dhcp_cfg.ip) then
      text = text..'net_connect ... OK <br>'
      text = text..'DHCP ... FAILED<br>No DHCP server answered. Check the cable/router or type a fixed IP.'
      ok = nil
   else
      text = text..'net_connect ... OK <br>'
      if use_dhcp then
	 ip, mask, gw = dhcp_cfg.ip, dhcp_cfg.mask, dhcp_cfg.gw
	 if dhcp_cfg.dns and dhcp_cfg.dns ~= "0.0.0.0" then
	    dns = dhcp_cfg.dns
	 end
	 if not mask or mask == "0.0.0.0" then
	    mask = "255.255.255.0"
	 end
	 text = text..'DHCP ... OK '..ip..' <br>'
	 text = text..'mask '..mask..' gw '..gw..' dns '..(dns or "?")..' <br>'
	 net_cfg.dhcp_ip = ip
      end
   end

   if ok then
      res = net_lwip_init(ip, mask, gw)
      if not res then
	 text = text..'lwip_init ... OK '
	 text = text..[[
<vspace h="16">You should be able to use the network from now on !
<vspace h="16">
To access files on your PC, you need dcload-ip, then launch it twice like this :
<font id="1" size="16" color="text_color"> <left>
<vspace h="16">
dc-tool -r <br>
dc-tool -z
<vspace h="16">
<font id="0" size="16"> <left>
On windowz, you can access only one of your drive, launch dc-tool from the drive
you want to access.
]]
	 if dns then
	    net_resolv(dns)
	 end
	 -- W5500 patch: background check of http streams for playlists
	 if not net_probe and home then
	    dl(home.."plugins/exe/net/netprobe.lez")
	 end
      else
	 text = text..'lwip_init ... FAILED'
      end
   end

   text = text.."<br><center>"

   hc()
   if scroll then dl_set_active(scroll, nil) end

   gui_ask(text, {"OK"})
end

