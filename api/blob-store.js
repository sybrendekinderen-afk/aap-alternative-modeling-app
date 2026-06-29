import { head, list, put } from '@vercel/blob';

const BLOB_PATH = 'app-data/data.json';
const BLOB_ACCESS = 'public';
const HANDLER_VERSION = '2026-06-29-public-access-v2';

function classifyBlobToken(token) {
  if (!token) return 'missing';
  if (token.startsWith('vercel_blob_rw_')) return 'read_write';
  if (token.startsWith('vercel_blob_client_')) return 'client';
  return 'unknown';
}

function buildDiagnostics(req) {
  const token = process.env.BLOB_READ_WRITE_TOKEN || '';
  return {
    method: req.method,
    blobPath: BLOB_PATH,
    blobAccess: BLOB_ACCESS,
    handlerVersion: HANDLER_VERSION,
    hasBlobToken: Boolean(token),
    blobTokenType: classifyBlobToken(token),
  };
}

async function readBlobText(blob) {
  if (blob.stream) {
    return await new Response(blob.stream).text();
  }

  const sourceUrl = blob.downloadUrl || blob.url;
  if (!sourceUrl) {
    return null;
  }

  const token = process.env.BLOB_READ_WRITE_TOKEN;
  const authHeaders = token ? { Authorization: `Bearer ${token}` } : undefined;

  let resp = await fetch(sourceUrl, {
    headers: authHeaders,
  });
  if (resp.status === 403 && authHeaders) {
    // Retry without auth in case the runtime provided a pre-signed downloadUrl.
    resp = await fetch(sourceUrl);
  }
  if (!resp.ok) {
    throw new Error(`Failed to fetch blob contents: ${resp.status}`);
  }
  return await resp.text();
}

async function findBlobMetadata(pathname) {
  if (typeof head === 'function') {
    try {
      return await head(pathname);
    } catch {
      // Fall through to list-based lookup when head is unavailable or path is missing.
    }
  }

  if (typeof list === 'function') {
    const result = await list({ prefix: pathname, limit: 1 });
    const found = result?.blobs?.find((b) => b.pathname === pathname);
    return found || null;
  }

  throw new Error('This @vercel/blob version does not provide head() or list().');
}

async function readRawRequestBody(req) {
  return await new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (chunk) => chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

async function parseBody(req) {
  // Do not touch req.body in @vercel/node; its getter may throw for non-JSON bodies.
  const rawBuffer = await readRawRequestBody(req);
  const rawText = rawBuffer.toString('utf8').trim();

  if (!rawText) {
    return { data: {}, rawText: '{}' };
  }

  try {
    return { data: JSON.parse(rawText), rawText };
  } catch (error) {
    const parseError = new SyntaxError('Invalid JSON body');
    parseError.details = error instanceof Error ? error.message : String(error);
    parseError.bodyPreview = rawText.slice(0, 250);
    throw parseError;
  }
}

export default async function handler(req, res) {
  try {
    if (req.method === 'GET') {
      res.setHeader('Cache-Control', 'no-store, max-age=0');
      const blob = await findBlobMetadata(BLOB_PATH);
      if (!blob) {
        return res.status(404).json({ error: 'Not found' });
      }

      const text = await readBlobText(blob);
      if (!text) {
        return res.status(404).json({ error: 'Not found' });
      }

      return res.status(200).json({ data: JSON.parse(text), etag: blob.etag });
    }

    if (req.method === 'PUT') {
      const { data } = await parseBody(req);

      const blob = await put(BLOB_PATH, JSON.stringify(data), {
        access: BLOB_ACCESS,
        addRandomSuffix: false,
        allowOverwrite: true,
        contentType: 'application/json',
      });

      res.setHeader('Cache-Control', 'no-store, max-age=0');

      return res.status(200).json({
        url: blob.url,
        pathname: blob.pathname,
        etag: blob.etag,
        handlerVersion: HANDLER_VERSION,
      });
    }

    res.setHeader('Allow', 'GET, PUT');
    return res.status(405).json({ error: 'Method not allowed' });
  } catch (error) {
    const diagnostics = buildDiagnostics(req);
    const message = error instanceof Error ? error.message : String(error);
    const isJsonParseError = error instanceof SyntaxError;

    console.error('blob-store error', {
      message,
      method: req?.method,
      diagnostics,
      stack: error?.stack || null,
      keys: error && typeof error === 'object' ? Object.keys(error) : [],
      statusCode: error?.statusCode || null,
      details: error?.details || null,
    });

    if (isJsonParseError) {
      return res.status(400).json({
        error: 'Invalid JSON body',
        details: error?.details || message,
        bodyPreview: error?.bodyPreview || null,
        diagnostics,
      });
    }

    const hint = diagnostics.hasBlobToken
      ? diagnostics.blobTokenType === 'client'
        ? 'BLOB_READ_WRITE_TOKEN is a client token. Replace it with the Blob Read/Write server token from Vercel Storage settings.'
        : 'Blob token seems present. Check Blob store connection and permissions in Vercel project settings.'
      : 'Missing BLOB_READ_WRITE_TOKEN in this deployment environment.';

    return res.status(500).json({
      error: message,
      errorType: error?.name || typeof error,
      errorStack: error?.stack ? String(error.stack).split('\n').slice(0, 3) : null,
      errorKeys: error && typeof error === 'object' ? Object.keys(error) : [],
      errorCause: error?.cause ? String(error.cause) : null,
      hint,
      diagnostics,
    });
  }
}
