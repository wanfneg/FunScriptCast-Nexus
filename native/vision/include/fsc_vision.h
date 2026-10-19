#pragma once
/* 原生视觉引擎 C ABI（首版，供宿主 ctypes 调用）。
 * 实现见 native/vision/src/engine.cpp；当前为骨架，未接 ORT。 */
#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    const char* video;
    double duration_ms;
    const char* bundle;
    const char* cache;
} fsc_vision_open_req;

void* fsc_vision_create(const char* config_json);
int   fsc_vision_open(void* h, const fsc_vision_open_req* req);
int   fsc_vision_state(void* h, char* buf, int cap);
int   fsc_vision_actions(void* h, double from_ms, double to_ms, char* buf, int cap);
void  fsc_vision_close(void* h);
void  fsc_vision_destroy(void* h);
const char* fsc_vision_version(void);

#ifdef __cplusplus
}
#endif
