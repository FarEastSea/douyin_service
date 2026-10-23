const TOKEN_KEY = "douyinAdminToken";

export class ApiError extends Error {
  status: number;
  code?: string;
  suggestion?: string;
  details?: Record<string, unknown>;
  fieldErrors: Record<string, string>;
  constructor(message: string, status = 0, payload: Record<string, any> = {}) {
    super(message);
    this.status = status;
    this.code = payload.code;
    this.suggestion = payload.suggestion;
    this.details = payload.details;
    this.fieldErrors = payload.field_errors || payload.details?.field_errors || {};
  }
}

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)?.trim() || "";
}
export function saveToken(token: string) {
  const value = token.trim();
  if (value) localStorage.setItem(TOKEN_KEY, value);
  else localStorage.removeItem(TOKEN_KEY);
}

export type ApiRequestOptions = RequestInit & { timeoutMs?: number };

export async function api<T = any>(
  path: string,
  init: ApiRequestOptions = {},
): Promise<T> {
  const { timeoutMs, ...requestInit } = init;
  const reading = ["GET", "HEAD"].includes((init.method || "GET").toUpperCase());
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort(init.signal?.reason);
  if (init.signal?.aborted) abortFromCaller();
  else init.signal?.addEventListener("abort", abortFromCaller, { once: true });
  let timedOut = false;
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs ?? (reading ? 15000 : 60000));
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type"))
    headers.set("Content-Type", "application/json");
  let response: Response;
  let text: string;
  try {
    response = await fetch(path.startsWith("/api") ? path : `/api${path}`, {
      ...requestInit,
      headers,
      signal: controller.signal,
    });
    text = await response.text();
  } catch (error) {
    if (timedOut)
      throw new ApiError(
        reading ? "读取超时，请重试" : "请求超时，操作结果待确认，请先刷新状态再决定是否重试",
        0,
        { code: "REQUEST_TIMEOUT" },
      );
    if (init.signal?.aborted) throw new DOMException("请求已取消", "AbortError");
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApiError(reading
      ? "无法连接服务，请检查网络或稍后重试"
      : "连接中断，操作结果待确认，请先刷新状态再决定是否重试");
  } finally {
    window.clearTimeout(timer);
    init.signal?.removeEventListener("abort", abortFromCaller);
  }
  let payload: any = {};
  const contentType = response.headers.get("content-type") || "";
  const isHtmlResponse =
    contentType.includes("text/html") || /^\s*<(?:!doctype|html)/i.test(text);
  let invalidJson = false;
  try {
    payload = text && !isHtmlResponse ? JSON.parse(text) : {};
  } catch {
    invalidJson = true;
    payload = {};
  }
  const objectPayload =
    payload && typeof payload === "object" && !Array.isArray(payload)
      ? payload
      : {};
  const detail = objectPayload.detail;
  const errorPayload =
    detail && typeof detail === "object" && !Array.isArray(detail)
      ? { ...objectPayload, ...detail }
      : objectPayload;
  if (!response.ok) {
    const businessAuthError = [
      "account_isolated",
      "browser_identity_missing",
      "cookie_invalid",
    ].includes(String(errorPayload.code || ""));
    if (response.status === 401 && !businessAuthError)
      window.dispatchEvent(new CustomEvent("app:auth-required"));
    const gatewayMessage =
      response.status === 504
        ? "请求超时，后台操作可能仍在继续，请稍后刷新任务状态"
        : `服务暂时不可用 (${response.status})`;
    const validation = Array.isArray(detail)
      ? detail
          .map((item: any) =>
            [
              Array.isArray(item.loc)
                ? item.loc.filter((part: unknown) => part !== "body").join(".")
                : "",
              typeof item.msg === "string" ? item.msg : "参数无效",
            ]
              .filter(Boolean)
              .join("："),
          )
          .join("；")
      : "";
    if (Array.isArray(detail)) {
      errorPayload.field_errors = Object.fromEntries(detail
        .filter((item: any) => Array.isArray(item.loc) && item.loc.length)
        .map((item: any) => [String(item.loc.at(-1)), String(item.msg || "参数无效")]));
    }
    const message =
      typeof errorPayload.message === "string"
        ? errorPayload.message
        : typeof detail === "string"
          ? detail
          : validation ||
            (isHtmlResponse ? gatewayMessage : `请求失败 (${response.status})`);
    throw new ApiError(message, response.status, errorPayload);
  }
  if (isHtmlResponse || invalidJson)
    throw new ApiError(
      "服务返回了非预期内容，请刷新或检查访问地址",
      response.status,
    );
  return payload as T;
}

export const jsonBody = (value: unknown): RequestInit => ({
  body: JSON.stringify(value),
});
