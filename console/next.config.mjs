// 静态导出:控制面在 /console/ 下托管这些文件,同源,不需要 Node 服务(D51)。API 在根路径,所以页面都在 basePath 下,不会和 /sandboxes 这类 API 路径撞。
// 本地开发:CP_URL=http://127.0.0.1:18700 npm run dev,下面的 rewrites 把 API 路径转给控制面(静态导出时 rewrites 不生效,也不需要)。
const CP = process.env.CP_URL
const API = ['sandboxes', 'accounts', 'projects', 'me', 'health', 'metrics', 'audit', 'nodes', 'volumes', 'builds', 'layers', 'templates', 'network', 'metering', 'fleet', 'routes', 'events', 'store', 'login', 'users']
const config = {
  output: CP ? undefined : 'export',
  basePath: '/console',
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
  ...(CP ? { async rewrites() { return API.map(p => ({ source: `/${p}/:path*`, destination: `${CP}/${p}/:path*`, basePath: false })).concat(API.map(p => ({ source: `/${p}`, destination: `${CP}/${p}`, basePath: false }))) } } : {}),
}
export default config
