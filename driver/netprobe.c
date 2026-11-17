/*
 * netprobe.c - dcplaya EXE plugin: asynchronous HTTP stream probe.
 *
 * Problem: dcplaya's playa_play() opens /http/... streams synchronously in
 * the Lua/UI thread.  A dead server makes lwIP's connect() block until its
 * TCP timeout, freezing the whole interface; a playlist full of dead links
 * freezes it until the end of the list.
 *
 * This plugin lets NET.LUA/SONG_BROWSER.LUA check a stream in a background
 * KOS thread first, so the UI keeps running (and the playlist can be stopped)
 * while a link is tested:
 *
 *   net_probe(path)        start probing "/http/host[:port]/path"
 *   net_probe_status()     nil = pending, 1 = server answered, 0,msg = failed
 *   net_probe_cancel()     forget the current probe
 *
 * Built against the 2004 dcplaya ABI (see oldkos.h): any_driver_t is 52
 * bytes, Lua commands 28 bytes, Lua 4.0 numbers are single floats.  lwIP and
 * dns() live in NET.LEZ and are looked up at call time, so this plugin can be
 * loaded at any moment without unresolved symbols.
 *
 * NOTE: lwip_select() must NOT be used: in this 2004 lwIP it leaves a
 * select callback pointing into the caller's stack, and later network events
 * corrupt memory (crashed the console inside an interrupt during video
 * streaming).  recv() in NET.LEZ has its own timeout; the UI side gives up
 * after 12 s anyway.
 */
#include "oldkos.h"

typedef struct lua_State lua_State;
typedef int (*lua_CFunction)(lua_State *L);

extern const char *lua_tostring(lua_State *L, int idx);
extern void lua_pushnumber(lua_State *L, double n);   /* float with -m4-single-only */
extern void lua_pushstring(lua_State *L, const char *s);
extern void lua_pushnil(lua_State *L);
extern void *malloc(unsigned int n);
extern void free(void *p);
extern int  sprintf(char *buf, const char *fmt, ...);
extern void *lef_find_symbol_all(const char *name);

/* --------------------------------------------------------------- dcplaya */

typedef struct {
    const char *name, *short_name, *topic, *usage;
    int type;                       /* 1 = C function */
    lua_CFunction function;
    int registered;
} luacmd_t;

typedef struct any_driver {
    struct any_driver *nxt;
    int type;
    int version;
    const char *name, *authors, *description;
    void *dll;
    int (*init)(struct any_driver *);
    int (*shutdown)(struct any_driver *);
    void *(*options)(struct any_driver *, int, void *);
    luacmd_t *luacommands;
    int count;
    void *mutex;
} any_driver_t;

_Static_assert(sizeof(luacmd_t) == 28, "luashell command size");
_Static_assert(sizeof(any_driver_t) == 52, "any_driver_t size");

#define EXE_DRIVER 0x00455845      /* 'EXE\0' */

/* ------------------------------------------------------------ lwIP (NET) */

struct sockaddr_in_l {
    uint8  sin_len;
    uint8  sin_family;
    uint16 sin_port;
    uint32 sin_addr;
    char   sin_zero[8];
};

static int (*p_socket)(int, int, int);
static int (*p_connect)(int, void *, int);
static int (*p_send)(int, const void *, int, unsigned int);
static int (*p_recv)(int, void *, int, unsigned int);
static int (*p_close)(int);
static int (*p_dns)(const char *, uint32 *);

static int resolve_net(void)
{
    if (p_socket && p_connect && p_send && p_recv && p_close && p_dns)
        return 0;
    p_socket  = (int (*)(int, int, int))lef_find_symbol_all("_lwip_socket");
    p_connect = (int (*)(int, void *, int))lef_find_symbol_all("_lwip_connect");
    p_send    = (int (*)(int, const void *, int, unsigned int))lef_find_symbol_all("_lwip_send");
    p_recv    = (int (*)(int, void *, int, unsigned int))lef_find_symbol_all("_lwip_recv");
    p_close   = (int (*)(int))lef_find_symbol_all("_lwip_close");
    p_dns     = (int (*)(const char *, uint32 *))lef_find_symbol_all("_dns");
    return (p_socket && p_connect && p_send && p_recv && p_close && p_dns) ? 0 : -1;
}

/* --------------------------------------------------------------- probing */

#define URL_MAX 512

typedef struct {
    int gen;
    char url[URL_MAX];
} job_t;

static volatile int cur_gen;        /* generation Lua is waiting for */
static volatile int res_gen;        /* generation of the last result */
static volatile int res_state;      /* 1 ok, 2 failed */
static char res_msg[96];
static volatile int workers;

static int slen(const char *s) { int n = 0; while (s[n]) n++; return n; }

static int prefix(const char *s, const char *p)
{
    while (*p)
        if (*s++ != *p++)
            return 0;
    return 1;
}

/* "/http/host[:port][/path]" or "http://host..." -> host, port, path */
static int parse_url(const char *u, char *host, int hmax, int *port, char *path, int pmax)
{
    int i = 0, n;

    if (prefix(u, "/http/"))
        u += 6;
    else if (prefix(u, "http://"))
        u += 7;
    else
        return -1;

    while (*u && *u != ':' && *u != '/' && *u != '<' && i < hmax - 1)
        host[i++] = *u++;
    host[i] = 0;
    if (!i)
        return -1;

    *port = 80;
    if (*u == ':') {
        u++;
        n = 0;
        while (*u >= '0' && *u <= '9')
            n = n * 10 + (*u++ - '0');
        if (n > 0 && n < 65536)
            *port = n;
    }

    i = 0;
    if (*u != '/')
        path[i++] = '/';
    while (*u && *u != '<' && i < pmax - 1)
        path[i++] = *u++;
    path[i] = 0;
    return 0;
}

/* returns 1 if the server answered with a 2xx/3xx (or ICY 200) status */
static int probe(const char *url, char *msg)
{
    char host[128], path[384], req[600], line[96];
    struct sockaddr_in_l sa;
    uint32 addr = 0;
    int port, s, n, got = 0, i;
    const char *st;

    if (resolve_net() < 0) {
        sprintf(msg, "network not ready");
        return 0;
    }
    if (parse_url(url, host, sizeof(host), &port, path, sizeof(path)) < 0) {
        sprintf(msg, "bad url");
        return 0;
    }
    if (p_dns(host, &addr) || !addr) {
        sprintf(msg, "dns failed for %s", host);
        return 0;
    }

    memset(&sa, 0, sizeof(sa));
    sa.sin_len = sizeof(sa);
    sa.sin_family = 2;                                   /* AF_INET */
    sa.sin_port = (uint16)(((port & 0xFF) << 8) | ((port >> 8) & 0xFF));
    sa.sin_addr = addr;                                  /* network order */

    s = p_socket(2, 1, 0);                               /* SOCK_STREAM */
    if (s < 0) {
        sprintf(msg, "no socket");
        return 0;
    }
    if (p_connect(s, &sa, sizeof(sa))) {
        p_close(s);
        sprintf(msg, "connect failed");
        return 0;
    }

    n = sprintf(req, "GET %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: dcplaya\r\n"
                     "Icy-MetaData: 0\r\nConnection: close\r\n\r\n", path, host);
    if (p_send(s, req, n, 0) != n) {
        p_close(s);
        sprintf(msg, "send failed");
        return 0;
    }

    while (got < (int)sizeof(line) - 1) {
        n = p_recv(s, line + got, sizeof(line) - 1 - got, 0);
        if (n <= 0)
            break;
        got += n;
        line[got] = 0;
        for (i = 0; i < got; i++)
            if (line[i] == '\n')
                break;
        if (i < got)
            break;
    }
    p_close(s);
    line[got] = 0;
    for (i = 0; i < got; i++)
        if (line[i] == '\r' || line[i] == '\n') { line[i] = 0; break; }

    if (!got) {
        sprintf(msg, "connection closed");
        return 0;
    }
    /* "ICY 200 OK", "HTTP/1.0 200 OK", "HTTP/1.1 302 Found" ... */
    st = line;
    while (*st && *st != ' ')
        st++;
    while (*st == ' ')
        st++;
    if ((prefix(line, "ICY") || prefix(line, "HTTP/")) && (*st == '2' || *st == '3'))
        return 1;
    line[60] = 0;
    sprintf(msg, "%s", line);
    return 0;
}

static void worker(void *arg)
{
    job_t *j = (job_t *)arg;
    char msg[96];
    int ok;

    msg[0] = 0;
    ok = probe(j->url, msg);
    if (j->gen == cur_gen) {
        memcpy(res_msg, msg, sizeof(res_msg));
        res_state = ok ? 1 : 2;
        res_gen = j->gen;            /* published last */
    }
    free(j);
    workers--;
}

/* ------------------------------------------------------------ Lua side */

static int lua_net_probe(lua_State *L)
{
    const char *u = lua_tostring(L, 1);
    job_t *j;
    kthread_t *t;
    int n;

    if (!u) {
        lua_pushnil(L);
        return 1;
    }
    j = (job_t *)malloc(sizeof(job_t));
    if (!j) {
        lua_pushnil(L);
        return 1;
    }
    n = slen(u);
    if (n >= URL_MAX)
        n = URL_MAX - 1;
    memcpy(j->url, u, n);
    j->url[n] = 0;
    j->gen = ++cur_gen;
    workers++;
    t = thd_create(worker, j);
    if (!t) {
        workers--;
        free(j);
        lua_pushnil(L);
        return 1;
    }
    thd_set_label(t, "net-probe");
    lua_pushnumber(L, 1);
    return 1;
}

static int lua_net_probe_status(lua_State *L)
{
    if (res_gen != cur_gen) {
        lua_pushnil(L);                      /* still running */
        return 1;
    }
    if (res_state == 1) {
        lua_pushnumber(L, 1);
        return 1;
    }
    lua_pushnumber(L, 0);
    lua_pushstring(L, res_msg);
    return 2;
}

static int lua_net_probe_cancel(lua_State *L)
{
    (void)L;
    ++cur_gen;                               /* orphan the running probe */
    return 0;
}

/* Measurement builds: counters exported by BBA.LEZ (-DW5500_STATS).
   Returns "us=..._rx=..._dl=..._fr=..._by=..._po=..._id=..." or nil. */
static int lua_net_w5500_stats(lua_State *L)
{
    uint32 *st = (uint32 *)lef_find_symbol_all("_w5500_stats");
    uint64 (*now)(void) = (uint64 (*)(void))lef_find_symbol_all("_timer_micro_gettime64");
    char buf[160];

    if (!st || !now) {
        lua_pushnil(L);
        return 1;
    }
    sprintf(buf, "us=%u_rx=%u_dl=%u_fr=%u_by=%u_po=%u_id=%u",
            (unsigned)(uint32)now(), (unsigned)st[0], (unsigned)st[1], (unsigned)st[2],
            (unsigned)st[3], (unsigned)st[4], (unsigned)st[5]);
    lua_pushstring(L, buf);
    return 1;
}

static luacmd_t commands[] = {
    { "net_w5500_stats", 0, 0,
      "net_w5500_stats() : W5500 driver CPU counters (measurement builds only).",
      1, lua_net_w5500_stats, 0 },
    { "net_probe", 0, 0,
      "net_probe(path) : start checking an http stream in background.",
      1, lua_net_probe, 0 },
    { "net_probe_status", 0, 0,
      "net_probe_status() : nil while running, 1 if the server answered, 0,msg on failure.",
      1, lua_net_probe_status, 0 },
    { "net_probe_cancel", 0, 0,
      "net_probe_cancel() : forget the running probe.",
      1, lua_net_probe_cancel, 0 },
    { 0, 0, 0, 0, 0, 0, 0 }
};

static int drv_init(any_driver_t *d)
{
    (void)d;
    return 0;
}

static int drv_shutdown(any_driver_t *d)
{
    int i;
    (void)d;
    ++cur_gen;
    for (i = 0; i < 300 && workers > 0; i++)   /* let workers finish (<=3s) */
        thd_sleep(10);
    return 0;
}

static void *drv_options(any_driver_t *d, int idx, void *o)
{
    (void)d; (void)idx;
    return o;
}

any_driver_t netprobe_driver = {
    0, EXE_DRIVER, 0x0100,
    "netprobe", "w5500 patch", "Asynchronous http stream probe",
    0, drv_init, drv_shutdown, drv_options, commands, 0, 0
};

any_driver_t *lef_main(void)
{
    return &netprobe_driver;
}
