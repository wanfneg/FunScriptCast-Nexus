/** 最小 API 客户端：统一错误处理，后续可换成自动生成的类型化客户端。 */

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal, headers: { Accept: "application/json" } });
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new ApiError(`响应不是 JSON（${res.status}）: ${text.slice(0, 200)}`, res.status);
  }
  if (!res.ok) {
    const msg = (data as { error?: string } | null)?.error ?? `请求失败（${res.status}）`;
    throw new ApiError(msg, res.status);
  }
  return data as T;
}

export type SystemInfo = {
  ok: boolean;
  app_root: string;
  version: string;
  python: string;
  platform: string;
  web_dist: { path: string; built: boolean };
  legacy_ui: boolean;
  runtime: Record<string, unknown>;
};
