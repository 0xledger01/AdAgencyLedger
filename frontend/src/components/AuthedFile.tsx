import { useEffect, useState } from "react";
import { fileBlobUrl } from "../api";

/**
 * 凭证 / 执照这类文件走的是 `GET /api/files/...`，需要 Bearer 头。
 * 而 `<img src>` 和 `window.open` 都带不了自定义头 —— 直接填 url 会 401。
 * 所以统一：先用 fetch 拿字节，转成 blob URL 再渲染 / 打开。
 */
export function useFileUrl(kind: "desk" | "portal", path: string | null | undefined) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!path) {
      setUrl("");
      setFailed(false);
      return;
    }
    let alive = true;
    fileBlobUrl(kind, path)
      .then((u) => {
        if (alive) setUrl(u);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [kind, path]);

  return { url, failed, loading: !!path && !url && !failed };
}

export function isPdf(path: string | null | undefined): boolean {
  return !!path && /\.pdf$/i.test(path);
}

/** 带鉴权的文件缩略图：图片显示图，PDF 显示 PDF 标；加载失败显示「无法读取」。 */
export function AuthedThumb({
  kind,
  path,
  alt,
  className = "voucher-thumb",
  onClick,
}: {
  kind: "desk" | "portal";
  path: string | null | undefined;
  alt: string;
  className?: string;
  onClick?: (blobUrl: string) => void;
}) {
  const { url, failed, loading } = useFileUrl(kind, path);

  if (!path) return <span className="faint">—</span>;
  if (failed) return <span className="voucher-miss">无法读取</span>;
  if (loading) return <span className="faint">加载中…</span>;
  if (isPdf(path)) {
    return (
      <button
        type="button"
        className={`${className} pdf`}
        title="查看 PDF"
        onClick={() => onClick?.(url)}
      >
        PDF
      </button>
    );
  }
  return (
    <button type="button" className={className} title="查看大图" onClick={() => onClick?.(url)}>
      <img src={url} alt={alt} />
    </button>
  );
}
