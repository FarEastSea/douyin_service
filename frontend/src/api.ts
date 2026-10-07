const TOKEN_KEY = "douyinAdminToken";

export class ApiError extends Error {
  status: number;
  code?: string;
  suggestion?: string;
  details?: Record<string, unknown>;
  constructor(message: string, status = 0, payload: Record<string, any> = {}) {
    super(message);
    this.status = status;
    this.code = payload.code;
    this.suggestion = payload.suggestion;
    this.details = payload.details;
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

export async function api<T = any>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type"))
    headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(path.startsWith("/api") ? path : `/api${path}`, {
      ...init,
      headers,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApiError("无法连接服务，请检查网络或稍后重试");
  }
  const text = await response.text();
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
