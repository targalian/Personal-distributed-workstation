/* Dashboard adapter; domain sources execute together with the original global contracts. */
const _meshClient = MeshTransport.create();
function _ensureMeshToken(){ return _meshClient.ensure(); }
function apiFetch(url,opts){ return _meshClient.request(url,opts); }
_ensureMeshToken();
