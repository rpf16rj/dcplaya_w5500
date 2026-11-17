/* oldkos.h - declarations matching the KallistiOS built into dcplaya 2.0b2
   (2004).  Do NOT include modern KOS headers in this plugin. */
#ifndef OLDKOS_H
#define OLDKOS_H

typedef unsigned char  uint8;
typedef unsigned short uint16;
typedef unsigned int   uint32;
typedef unsigned long long uint64;

#define NULL ((void *)0)

/* netif_t as laid out in the dcplaya kernel (76 bytes).  Offsets verified in
   BBA.LEZ::bba_init and kernel net_dev_init (detect=+40, init=+44,
   shutdown=+48, start=+52, stop=+56). */
typedef struct knetif {
    struct knetif  *le_next;        /* +0  LIST_ENTRY */
    struct knetif **le_prev;        /* +4  */
    const char *name;               /* +8  */
    const char *descr;              /* +12 */
    int    index;                   /* +16 */
    uint32 dev_id;                  /* +20 */
    uint32 flags;                   /* +24 */
    uint8  mac_addr[6];             /* +28 */
    uint8  ip_addr[4];              /* +34 */
    uint8  _pad[2];                 /* +38 */
    int (*if_detect)(struct knetif *self);                      /* +40 */
    int (*if_init)(struct knetif *self);                        /* +44 */
    int (*if_shutdown)(struct knetif *self);                    /* +48 */
    int (*if_start)(struct knetif *self);                       /* +52 */
    int (*if_stop)(struct knetif *self);                        /* +56 */
    int (*if_tx)(struct knetif *self, const uint8 *data,
                 int len, int blocking);                        /* +60 */
    int (*if_tx_commit)(struct knetif *self);                   /* +64 */
    int (*if_rx_poll)(struct knetif *self);                     /* +68 */
    int (*if_set_flags)(struct knetif *self, uint32 flags_and,
                        uint32 flags_or);                       /* +72 */
} netif_t;

_Static_assert(sizeof(netif_t) == 76, "old KOS netif_t must be 76 bytes");

#define NETIF_REGISTERED   0x00000001
#define NETIF_DETECTED     0x00000002
#define NETIF_INITIALIZED  0x00000004
#define NETIF_RUNNING      0x00000008
#define NETIF_PROMISC      0x00010000

#define NETIF_TX_OK     0
#define NETIF_TX_ERROR -1
#define NETIF_TX_AGAIN -2

typedef struct kthread kthread_t;
typedef struct semaphore semaphore_t;
typedef struct lef_prog lef_prog_t;

/* Kernel / dcplaya exports (resolved by name from main_symtab). */
extern int  printf(const char *fmt, ...);
extern void *memcpy(void *d, const void *s, unsigned int n);
extern void *memset(void *d, int c, unsigned int n);
extern kthread_t *thd_create(void (*routine)(void *param), void *param);
extern int  thd_set_label(kthread_t *thd, const char *label);
extern void thd_sleep(int ms);
extern void thd_pass(void);
extern semaphore_t *sem_create(int value);
extern void sem_destroy(semaphore_t *sem);
extern int  sem_wait(semaphore_t *sem);
extern int  sem_trywait(semaphore_t *sem);
extern void sem_signal(semaphore_t *sem);
extern int  irq_inside_int(void);
extern int  irq_disable(void);
extern void irq_restore(int v);
extern int  net_input(netif_t *device, const uint8 *data, int len);
extern int  net_reg_device(netif_t *device);
extern int  net_unreg_device(netif_t *device);
extern int  scif_init(void);
extern int  dcload_type;
extern lef_prog_t *lef_load(const char *name);
extern void *lef_find_symbol(lef_prog_t *p, const char *name);
extern void lef_free(lef_prog_t *p);
extern char *shell_home_path(char *buf, int size, const char *path);

#define DCLOAD_TYPE_SER 0

extern int memcmp(const void *a, const void *b, unsigned int n);

/* dhcp.c */
int  dhcp_requested(void);
void dhcp_run(netif_t *nif, int (*tx)(netif_t *, const uint8 *, int, int));
void dhcp_fixup(void);
void dhcp_reset(void);

#endif
