import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getPrivacyPreviewImage,
  getPrivacyPreviewStatus,
  getRtspDebugConfig,
  getRtspDebugPreview,
  updateRtspDebugConfig,
} from "@/api";
import type { PrivacyPreviewStatus, RtspDebugConfig } from "@/lib/types";
import { toast } from "./Toast";

const INPUT_CLS =
  "w-full px-3 py-2 rounded-lg bg-bg-primary border border-border " +
  "focus:border-brand-primary focus:outline-none text-text-primary num";

function StatusChip({
  tone,
  label,
}: {
  tone: "ok" | "warn" | "muted";
  label: string;
}) {
  const cls =
    tone === "ok"
      ? "bg-success/10 text-success"
      : tone === "warn"
        ? "bg-warning/10 text-warning"
        : "bg-bg-primary text-text-secondary";
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-caption ${cls}`}>
      {label}
    </span>
  );
}

function useBlobUrl(
  kind: "rtsp" | "privacy-original" | "privacy-processed" | null,
  refreshKey: number,
) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!kind) {
      setUrl((old) => {
        if (old) URL.revokeObjectURL(old);
        return null;
      });
      return;
    }
    let cancelled = false;
    let nextUrl: string | null = null;
    const loader =
      kind === "rtsp"
        ? getRtspDebugPreview
        : kind === "privacy-original"
          ? () => getPrivacyPreviewImage("original")
          : () => getPrivacyPreviewImage("processed");
    void loader()
      .then((blob) => {
        if (cancelled) return;
        nextUrl = URL.createObjectURL(blob);
        setUrl((old) => {
          if (old) URL.revokeObjectURL(old);
          return nextUrl;
        });
      })
      .catch(() => {
        if (cancelled) return;
        setUrl((old) => {
          if (old) URL.revokeObjectURL(old);
          return null;
        });
      });
    return () => {
      cancelled = true;
    };
  }, [kind, refreshKey]);
  return url;
}

export function UsageDebugStreams() {
  const { t } = useTranslation();
  const [config, setConfig] = useState<RtspDebugConfig | null>(null);
  const [privacy, setPrivacy] = useState<PrivacyPreviewStatus | null>(null);
  const [urlInput, setUrlInput] = useState("");
  const [nameInput, setNameInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [rtspRefreshKey, setRtspRefreshKey] = useState(0);
  const [privacyRefreshKey, setPrivacyRefreshKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [cfg, prv] = await Promise.all([
          getRtspDebugConfig(),
          getPrivacyPreviewStatus(),
        ]);
        if (cancelled) return;
        setConfig(cfg);
        setPrivacy(prv);
        setUrlInput(cfg.url ?? "");
        setNameInput(cfg.name);
      } catch (e) {
        if (cancelled) return;
        toast(e instanceof Error ? e.message : t("usage.debugLoadFailed"), "warn");
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [t]);

  useEffect(() => {
    const timer = window.setInterval(async () => {
      try {
        const cfg = await getRtspDebugConfig();
        setConfig(cfg);
        setRtspRefreshKey((k) => k + 1);
      } catch {
        // ignore polling errors
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const timer = window.setInterval(async () => {
      try {
        const next = await getPrivacyPreviewStatus();
        setPrivacy((prev) => {
          if (!prev || prev.timestampMs !== next.timestampMs) {
            setPrivacyRefreshKey((k) => k + 1);
          }
          return next;
        });
      } catch {
        // ignore polling errors
      }
    }, 5000);
    return () => window.clearInterval(timer);
  }, []);

  const rtspPreviewUrl = useBlobUrl(config?.hasPreview ? "rtsp" : null, rtspRefreshKey);
  const privacyOriginalUrl = useBlobUrl(
    privacy?.hasPreview ? "privacy-original" : null,
    privacyRefreshKey,
  );
  const privacyProcessedUrl = useBlobUrl(
    privacy?.hasPreview ? "privacy-processed" : null,
    privacyRefreshKey,
  );

  async function save(nextUrl: string | null) {
    setSaving(true);
    try {
      const data = await updateRtspDebugConfig({
        url: nextUrl,
        name: nameInput.trim() || "RTSP Camera",
      });
      setConfig(data);
      setUrlInput(data.url ?? "");
      setNameInput(data.name);
      setRtspRefreshKey((k) => k + 1);
      toast(t("usage.debugSaveSuccess"), "ok");
    } catch (e) {
      toast(e instanceof Error ? e.message : t("usage.debugSaveFailed"), "warn");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6">
      <div className="flex items-baseline justify-between gap-3 flex-wrap mb-4">
        <h2 className="text-section-title">{t("usage.debugTitle")}</h2>
        <div className="flex items-center gap-2">
          <StatusChip
            tone={config?.connected ? "ok" : config?.enabled ? "warn" : "muted"}
            label={
              config?.connected
                ? t("usage.debugConnected")
                : config?.enabled
                  ? t("usage.debugConfigured")
                  : t("usage.debugDisabled")
            }
          />
          <StatusChip
            tone={privacy?.hasPreview ? "ok" : "muted"}
            label={
              privacy?.hasPreview
                ? t("usage.debugPrivacyReady")
                : t("usage.debugPrivacyIdle")
            }
          />
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(320px,420px)_1fr]">
        <div className="space-y-4">
          <div>
            <label className="block mb-1 text-caption text-text-secondary">
              {t("usage.debugUrlLabel")}
            </label>
            <input
              value={urlInput}
              onChange={(e) => setUrlInput(e.target.value)}
              placeholder={t("usage.debugUrlPlaceholder")}
              className={INPUT_CLS}
            />
          </div>
          <div>
            <label className="block mb-1 text-caption text-text-secondary">
              {t("usage.debugNameLabel")}
            </label>
            <input
              value={nameInput}
              onChange={(e) => setNameInput(e.target.value)}
              placeholder="RTSP Camera"
              className={INPUT_CLS}
            />
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={saving}
              onClick={() => save(urlInput)}
              className="px-3 py-2 rounded-lg bg-bg-primary border border-border hover:border-border-strong text-text-primary disabled:opacity-40"
            >
              {saving ? t("usage.saving") : t("usage.save")}
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => save(null)}
              className="px-3 py-2 rounded-lg bg-bg-primary border border-border hover:border-border-strong text-text-secondary disabled:opacity-40"
            >
              {t("usage.debugDisable")}
            </button>
          </div>
          <div className="text-caption text-text-secondary space-y-1">
            <div>{t("usage.debugHintRtmp")}</div>
            {config?.lastError && (
              <div className="text-warning">
                {t("usage.debugLastError")}: {config.lastError}
              </div>
            )}
            <div>{t("usage.debugHintPrivacy")}</div>
          </div>
        </div>

        <div className="space-y-6">
          <div>
            <div className="text-caption text-text-tertiary mb-2">
              {t("usage.debugPreviewTitle")}
            </div>
            <div className="rounded-xl border border-border bg-bg-primary overflow-hidden aspect-video">
              {rtspPreviewUrl ? (
                <img
                  src={rtspPreviewUrl}
                  alt={config?.name ?? "RTSP preview"}
                  className="w-full h-full object-cover"
                />
              ) : (
                <div className="w-full h-full flex items-center justify-center text-text-secondary text-caption">
                  {t("usage.debugNoPreview")}
                </div>
              )}
            </div>
          </div>

          <div>
            <div className="flex items-baseline justify-between gap-2 mb-2 flex-wrap">
              <div className="text-caption text-text-tertiary">
                {t("usage.debugPrivacyTitle")}
              </div>
              <div className="text-caption text-text-secondary">
                {privacy?.pluginInstalled
                  ? privacy.debugEnabled
                    ? t("usage.debugPrivacyDebugOn")
                    : t("usage.debugPrivacyDebugOff")
                  : t("usage.debugPrivacyPluginMissing")}
              </div>
            </div>
            {privacy?.message && (
              <div className="mb-2 text-caption text-warning">{privacy.message}</div>
            )}
            <div className="grid gap-3 md:grid-cols-2">
              <PreviewPane
                title={t("usage.debugOriginal")}
                imageUrl={privacyOriginalUrl}
                empty={t("usage.debugPrivacyEmpty")}
              />
              <PreviewPane
                title={t("usage.debugProcessed")}
                imageUrl={privacyProcessedUrl}
                empty={t("usage.debugPrivacyEmpty")}
              />
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function PreviewPane({
  title,
  imageUrl,
  empty,
}: {
  title: string;
  imageUrl: string | null;
  empty: string;
}) {
  return (
    <div>
      <div className="text-caption text-text-secondary mb-2">{title}</div>
      <div className="rounded-xl border border-border bg-bg-primary overflow-hidden aspect-video">
        {imageUrl ? (
          <img src={imageUrl} alt={title} className="w-full h-full object-cover" />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-text-secondary text-caption">
            {empty}
          </div>
        )}
      </div>
    </div>
  );
}
