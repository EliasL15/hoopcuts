import { useEffect } from "react";
import { useLocation } from "react-router-dom";
import pages from "../public/seo.json";

export function Seo() {
  const { pathname } = useLocation();
  useEffect(() => {
    const page = pages[pathname as keyof typeof pages];
    const origin = document.querySelector<HTMLMetaElement>('meta[name="site-origin"]')?.content || window.location.origin;
    const title = page?.title || "Your private film room | HoopCuts";
    const description = page?.description || "Sign in to manage your private HoopCuts basketball game library and account.";
    document.title = title;
    const setMeta = (key: string, value: string, property = false) => {
      const attribute = property ? "property" : "name";
      let element = document.head.querySelector<HTMLMetaElement>(`meta[${attribute}="${key}"]`);
      if (!element) { element = document.createElement("meta"); element.setAttribute(attribute, key); document.head.append(element); }
      element.content = value;
    };
    setMeta("description", description);
    setMeta("robots", page ? "index, follow, max-image-preview:large" : "noindex, follow");
    setMeta("og:title", title, true); setMeta("og:description", description, true);
    setMeta("og:url", `${origin}${pathname}`, true);
    setMeta("og:image", `${origin}/assets/brand/hoopcuts-symbol.svg`, true);
    setMeta("og:image:secure_url", `${origin}/assets/brand/hoopcuts-symbol.svg`, true);
    setMeta("twitter:image", `${origin}/assets/brand/hoopcuts-symbol.svg`);
    setMeta("twitter:title", title); setMeta("twitter:description", description);
    let canonical = document.head.querySelector<HTMLLinkElement>('link[rel="canonical"]');
    if (page) {
      if (!canonical) { canonical = document.createElement("link"); canonical.rel = "canonical"; document.head.append(canonical); }
      canonical.href = `${origin}${pathname}`;
    } else canonical?.remove();
    // The server's page-specific schema must not survive navigation to another route.
    const schema = document.getElementById("seo-schema");
    if (schema && schema.getAttribute("data-path") !== pathname) schema.remove();
  }, [pathname]);
  return null;
}
