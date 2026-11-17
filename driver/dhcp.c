/*
 * dhcp.c - minimal DHCP client for the dcplaya "bba" network plugin.
 *
 * dcplaya 2.0b2's lwIP has no DHCP, and its Lua side only knows static
 * addresses.  This client runs inside the plugin right after the adapter is
 * started (both W5500 and the original RTL8139 BBA), talking raw Ethernet:
 *   - frames are sent with the adapter's own if_tx(),
 *   - replies are captured by temporarily owning the kernel net_input target
 *     (dcplaya's NET.LEZ installs its own target only after net_init()).
 *
 * Hand-off to Lua (NET.LUA):
 *   Lua creates  /ram/dhcp.req  before net_connect("0.0.0.0")
 *   we write     /ram/dhcp.lua  :  dhcp_cfg = { ip=..., ... }
 *   Lua then calls net_lwip_init()/net_resolv() with those values.
 * dcplaya's own small stack (packet.c, used by dc-tool/tcpfs) gets its IP
 * through NET.LEZ's eth_setip(), re-applied on transmit (see dhcp_fixup()).
 *
 * No lease renewal: leases are typically hours long, a dcplaya session isn't.
 */
#include "oldkos.h"

#define DHCP_REQ_FILE "/ram/dhcp.req"
#define DHCP_RES_FILE "/ram/dhcp.lua"

/* old KOS fs open modes (from libc convert_mode: "r"=1, "w"=0x104) */
#define OLD_O_RDONLY 1
#define OLD_O_WRITE  0x104

extern int  fs_open(const char *fn, int mode);
extern int  fs_write(int fd, const void *buf, int cnt);
extern void fs_close(int fd);
extern int  fs_unlink(const char *fn);
extern int  sprintf(char *buf, const char *fmt, ...);
extern void *lef_find_symbol_all(const char *name);
extern volatile uint32 jiffies;

typedef int (*net_input_func)(netif_t *dev, const uint8 *data, int len);
extern net_input_func net_input_set_target(net_input_func t);

/* DHCP message types */
#define DHCPDISCOVER 1
#define DHCPOFFER    2
#define DHCPREQUEST  3
#define DHCPACK      5
#define DHCPNAK      6

#define BOOTP_OFF   42          /* 14 eth + 20 ip + 8 udp */
#define OPT_OFF     (BOOTP_OFF + 240)

static uint8 txf[600] __attribute__((aligned(4)));
static uint8 rxf[1536] __attribute__((aligned(4)));
static volatile int rx_got;
static volatile int rx_len;
static uint32 cur_xid;
static const uint8 *cur_mac;

/* results */
static int   dhcp_ok;
static uint8 r_ip[4], r_mask[4], r_gw[4], r_dns[4], r_srv[4];
static uint32 r_lease;
static void (*eth_setip_fn)(int, int, int, int);

static void put16(uint8 *p, uint16 v) { p[0] = (uint8)(v >> 8); p[1] = (uint8)v; }
static void put32(uint8 *p, uint32 v)
{
    p[0] = (uint8)(v >> 24); p[1] = (uint8)(v >> 16);
    p[2] = (uint8)(v >> 8);  p[3] = (uint8)v;
}
static uint32 get32(const uint8 *p)
{
    return ((uint32)p[0] << 24) | ((uint32)p[1] << 16) | ((uint32)p[2] << 8) | p[3];
}

static uint16 ip_csum(const uint8 *p, int len)
{
    uint32 s = 0;
    int i;
    for (i = 0; i < len; i += 2)
        s += (uint32)((p[i] << 8) | p[i + 1]);
    while (s >> 16)
        s = (s & 0xFFFF) + (s >> 16);
    return (uint16)~s;
}

static int build(int type, const uint8 *mac, const uint8 *req_ip, const uint8 *srv)
{
    uint8 *b = txf + BOOTP_OFF;
    uint8 *o = txf + OPT_OFF;
    int blen, ulen, len;

    memset(txf, 0, sizeof(txf));
    /* Ethernet */
    memset(txf, 0xFF, 6);
    memcpy(txf + 6, mac, 6);
    txf[12] = 0x08; txf[13] = 0x00;

    /* BOOTP */
    b[0] = 1; b[1] = 1; b[2] = 6; b[3] = 0;
    put32(b + 4, cur_xid);
    put16(b + 10, 0x8000);                 /* ask for broadcast replies */
    memcpy(b + 28, mac, 6);                /* chaddr */
    b[236] = 0x63; b[237] = 0x82; b[238] = 0x53; b[239] = 0x63;

    *o++ = 53; *o++ = 1; *o++ = (uint8)type;
    *o++ = 61; *o++ = 7; *o++ = 1; memcpy(o, mac, 6); o += 6;
    *o++ = 12; *o++ = 7; memcpy(o, "dcplaya", 7); o += 7;
    if (req_ip) { *o++ = 50; *o++ = 4; memcpy(o, req_ip, 4); o += 4; }
    if (srv)    { *o++ = 54; *o++ = 4; memcpy(o, srv, 4); o += 4; }
    *o++ = 57; *o++ = 2; *o++ = 0x05; *o++ = 0xDC;      /* max msg 1500 */
    *o++ = 55; *o++ = 3; *o++ = 1; *o++ = 3; *o++ = 6;  /* mask, router, dns */
    *o++ = 255;

    blen = (int)(o - b);
    if (blen < 300)
        blen = 300;                         /* BOOTP minimum */
    ulen = 8 + blen;
    len = 14 + 20 + ulen;

    /* IPv4 */
    txf[14] = 0x45;
    put16(txf + 16, (uint16)(20 + ulen));
    put16(txf + 18, (uint16)cur_xid);
    txf[22] = 64; txf[23] = 17;
    memset(txf + 30, 0xFF, 4);              /* src 0.0.0.0, dst broadcast */
    put16(txf + 24, ip_csum(txf + 14, 20));

    /* UDP 68 -> 67, checksum 0 (allowed for IPv4) */
    put16(txf + 34, 68);
    put16(txf + 36, 67);
    put16(txf + 38, (uint16)ulen);
    return len;
}

/* Runs in the adapter's RX thread. */
static int dhcp_input(netif_t *dev, const uint8 *p, int len)
{
    int ihl;
    const uint8 *u, *b;

    (void)dev;
    if (rx_got || len < 14 + 20 + 8 + 240 || len > (int)sizeof(rxf))
        return 0;
    if (p[12] != 0x08 || p[13] != 0x00 || (p[14] >> 4) != 4 || p[23] != 17)
        return 0;
    ihl = (p[14] & 0x0F) * 4;
    u = p + 14 + ihl;
    if (u + 8 + 240 > p + len)
        return 0;
    if (u[0] != 0 || u[1] != 67 || u[2] != 0 || u[3] != 68)
        return 0;
    b = u + 8;
    if (b[0] != 2 || get32(b + 4) != cur_xid || memcmp(b + 28, cur_mac, 6))
        return 0;
    memcpy(rxf, b, (int)(p + len - b));
    rx_len = (int)(p + len - b);
    rx_got = 1;
    return 0;
}

/* Parse captured BOOTP reply; returns DHCP message type or 0. */
static int parse(uint8 *yi, uint8 *mask, uint8 *gw, uint8 *dns, uint8 *srv, uint32 *lease)
{
    const uint8 *b = rxf, *o, *end;
    int type = 0;

    if (rx_len < 240 || b[236] != 0x63 || b[237] != 0x82 || b[238] != 0x53 || b[239] != 0x63)
        return 0;
    memcpy(yi, b + 16, 4);
    o = b + 240;
    end = b + rx_len;
    while (o < end && *o != 255) {
        uint8 c = o[0], l;
        if (c == 0) { o++; continue; }
        if (o + 2 > end) break;
        l = o[1];
        if (o + 2 + l > end) break;
        switch (c) {
        case 53: if (l >= 1) type = o[2]; break;
        case 1:  if (l >= 4) memcpy(mask, o + 2, 4); break;
        case 3:  if (l >= 4) memcpy(gw, o + 2, 4); break;
        case 6:  if (l >= 4) memcpy(dns, o + 2, 4); break;
        case 54: if (l >= 4) memcpy(srv, o + 2, 4); break;
        case 51: if (l >= 4) *lease = get32(o + 2); break;
        }
        o += 2 + l;
    }
    return type;
}

/* Wait up to ms for a reply of one of the wanted types. */
static int wait_reply(int want1, int want2, int ms,
                      uint8 *yi, uint8 *mask, uint8 *gw, uint8 *dns, uint8 *srv, uint32 *lease)
{
    int t;
    for (t = 0; t < ms; t += 10) {
        thd_sleep(10);
        if (rx_got) {
            int type = parse(yi, mask, gw, dns, srv, lease);
            rx_got = 0;
            if (type == want1 || type == want2)
                return type;
        }
    }
    return 0;
}

static void ipstr(char *s, const uint8 *a)
{
    sprintf(s, "%d.%d.%d.%d", a[0], a[1], a[2], a[3]);
}

static void write_result(void)
{
    char buf[256], a[16], m[16], g[16], d[16], s[16];
    int fd, n;

    fs_unlink(DHCP_RES_FILE);
    fd = fs_open(DHCP_RES_FILE, OLD_O_WRITE);
    if (fd < 0) {
        printf("dhcp: cannot write %s\n", DHCP_RES_FILE);
        return;
    }
    if (dhcp_ok) {
        ipstr(a, r_ip); ipstr(m, r_mask); ipstr(g, r_gw); ipstr(d, r_dns); ipstr(s, r_srv);
        n = sprintf(buf, "dhcp_cfg = { ip=\"%s\", mask=\"%s\", gw=\"%s\", dns=\"%s\", server=\"%s\", lease=%d }\n",
                    a, m, g, d, s, (int)r_lease);
    } else {
        n = sprintf(buf, "dhcp_cfg = { failed=1 }\n");
    }
    fs_write(fd, buf, n);
    fs_close(fd);
}

int dhcp_requested(void)
{
    int fd = fs_open(DHCP_REQ_FILE, OLD_O_RDONLY);
    if (fd < 0)
        return 0;
    fs_close(fd);
    return 1;
}

void dhcp_run(netif_t *nif, int (*tx)(netif_t *, const uint8 *, int, int))
{
    net_input_func old;
    uint8 yi[4], mask[4], gw[4], dns[4], srv[4];
    uint32 lease = 0;
    int attempt, len, type;

    dhcp_ok = 0;
    eth_setip_fn = NULL;
    cur_mac = nif->mac_addr;
    cur_xid = ((uint32)nif->mac_addr[2] << 24 | (uint32)nif->mac_addr[3] << 16 |
               (uint32)nif->mac_addr[4] << 8 | nif->mac_addr[5]) ^ (jiffies * 2654435761u);
    rx_got = 0;
    old = net_input_set_target(dhcp_input);

    printf("dhcp: discovering...\n");
    for (attempt = 0; attempt < 4 && !dhcp_ok; attempt++) {
        memset(yi, 0, 4); memset(mask, 0, 4); memset(gw, 0, 4);
        memset(dns, 0, 4); memset(srv, 0, 4);
        cur_xid++;
        len = build(DHCPDISCOVER, nif->mac_addr, NULL, NULL);
        rx_got = 0;
        tx(nif, txf, len, 1);
        if (wait_reply(DHCPOFFER, -1, 2000 + attempt * 1000,
                       yi, mask, gw, dns, srv, &lease) != DHCPOFFER)
            continue;

        len = build(DHCPREQUEST, nif->mac_addr, yi, srv);
        rx_got = 0;
        tx(nif, txf, len, 1);
        type = wait_reply(DHCPACK, DHCPNAK, 3000, yi, mask, gw, dns, srv, &lease);
        if (type == DHCPACK) {
            memcpy(r_ip, yi, 4); memcpy(r_mask, mask, 4); memcpy(r_gw, gw, 4);
            memcpy(r_dns, dns, 4); memcpy(r_srv, srv, 4);
            r_lease = lease;
            dhcp_ok = 1;
        }
    }

    net_input_set_target(old);

    if (dhcp_ok) {
        memcpy(nif->ip_addr, r_ip, 4);
        eth_setip_fn = (void (*)(int, int, int, int))lef_find_symbol_all("_eth_setip");
        printf("dhcp: got %d.%d.%d.%d gw %d.%d.%d.%d dns %d.%d.%d.%d\n",
               r_ip[0], r_ip[1], r_ip[2], r_ip[3], r_gw[0], r_gw[1], r_gw[2], r_gw[3],
               r_dns[0], r_dns[1], r_dns[2], r_dns[3]);
    } else {
        printf("dhcp: no answer from a DHCP server\n");
    }
    write_result();
}

/* NET.LEZ's net_connect() sets its small stack's IP from the Lua argument
   ("0.0.0.0" in DHCP mode) after we return; re-apply the leased address on
   every transmit.  eth_setip() only stores 4 bytes, safe in any context. */
void dhcp_fixup(void)
{
    if (eth_setip_fn)
        eth_setip_fn(r_ip[0], r_ip[1], r_ip[2], r_ip[3]);
}

void dhcp_reset(void)
{
    eth_setip_fn = NULL;
    dhcp_ok = 0;
}
