#include "fsc_vision.h"
#include <cstdio>
#include <cstring>

/* 阶段 0 骨架：ABI 已固定，推理在阶段 1 接 ORT。 */
void* fsc_vision_create(const char*) { return (void*)0x1; }
int fsc_vision_open(void*, const fsc_vision_open_req*) { return -1; }
int fsc_vision_state(void*, char* buf, int cap) {
    if (buf && cap > 0) std::snprintf(buf, (size_t)cap, "{\"ok\":false,\"state\":\"unimplemented\"}");
    return -1;
}
int fsc_vision_actions(void*, double, double, char* buf, int cap) {
    if (buf && cap > 0) std::snprintf(buf, (size_t)cap, "{\"ok\":false,\"actions\":[]}");
    return -1;
}
void fsc_vision_close(void*) {}
void fsc_vision_destroy(void*) {}
const char* fsc_vision_version(void) { return "0.1.0-skeleton"; }
