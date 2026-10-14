#ifndef LWIP_ARCH_CC_H
#define LWIP_ARCH_CC_H

#define LWIP_UNIX_LINUX
#define LWIP_TIMEVAL_PRIVATE 0
#define LWIP_DONT_PROVIDE_BYTEORDER_FUNCTIONS
#include <sys/time.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define LWIP_ERRNO_INCLUDE <errno.h>
#define LWIP_ERRNO_STDINCLUDE 1

unsigned int lwip_port_rand(void);
#define LWIP_RAND() (lwip_port_rand())

typedef unsigned int sys_prot_t;

#endif
