import { renderToString } from "react-dom/server";
import { StaticRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Landing } from "./App";
import { CustomerInfo, type InfoPage } from "./CustomerInfo";
import type { ProductApi } from "./types";

export function render(path: string) {
  const api = { mode: "hosted", plans: async () => ({ enabled: true, currency: "usd", plans: [] }), account: async () => { throw new Error("No account during public pre-rendering"); } } as unknown as ProductApi;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, queryFn: async () => null } } });
  return renderToString(<StaticRouter location={path}><QueryClientProvider client={queryClient}>
    {path === "/" ? <Landing api={api} signedIn={false} /> : <CustomerInfo page={path.slice(1) as InfoPage} />}
  </QueryClientProvider></StaticRouter>);
}
