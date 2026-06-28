import { get, put } from '@vercel/blob';

const BLOB_PATH = 'app-data/data.json';

export async function GET() {
  try {
    const blob = await get(BLOB_PATH, { access: 'private' });
    if (!blob || !blob.stream) {
      return Response.json({ error: 'Not found' }, { status: 404 });
    }

    const text = await new Response(blob.stream).text();
    return Response.json({ data: JSON.parse(text), etag: blob.etag });
  } catch (error) {
    return Response.json(
      { error: error instanceof Error ? error.message : String(error) },
      { status: 500 },
    );
  }
}

export async function PUT(request) {
  try {
    const data = await request.json();
    const ifMatch = request.headers.get('if-match') || undefined;
    const blob = await put(BLOB_PATH, JSON.stringify(data), {
      access: 'private',
      allowOverwrite: true,
      cacheControlMaxAge: 60,
      contentType: 'application/json',
      ifMatch,
    });

    return Response.json({ url: blob.url, pathname: blob.pathname, etag: blob.etag }, { status: 200 });
  } catch (error) {
    return Response.json(
      { error: error instanceof Error ? error.message : String(error) },
      { status: 500 },
    );
  }
}
