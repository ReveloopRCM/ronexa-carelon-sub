import { MongoClient, Db } from 'mongodb';

let cachedClient: MongoClient | null = null;
let cachedDb: Db | null = null;

const MONGODB_URI = process.env.MONGODB_URI;
const MONGODB_DB_NAME = process.env.MONGODB_DB_NAME || 'llm_orchestration_prod';

if (!MONGODB_URI) {
  throw new Error('MONGODB_URI environment variable is not set');
}

export async function connectToDatabase() {
  if (cachedClient && cachedDb) {
    return { client: cachedClient, db: cachedDb };
  }

  try {
    // The module-level `if (!MONGODB_URI) throw` above guarantees this is
    // defined by the time any caller reaches here, but TS doesn't carry
    // narrowing across the closure boundary into this function. The `!`
    // makes the guarantee explicit so `next build` typechecks.
    const client = new MongoClient(MONGODB_URI!);
    await client.connect();
    const db = client.db(MONGODB_DB_NAME);

    cachedClient = client;
    cachedDb = db;

    return { client, db };
  } catch (error) {
    console.error('MongoDB connection failed:', error);
    throw error;
  }
}

export async function queryGatewaySubmissions(params?: {
  workflow_id?: string;
  workflow_type?: string;
  page?: number;
  pageSize?: number;
}) {
  const { db } = await connectToDatabase();
  const collection = db.collection('gateway_submissions');

  const query: Record<string, any> = {};
  if (params?.workflow_id) query.workflow_id = params.workflow_id;
  if (params?.workflow_type) query.workflow_type = params.workflow_type;

  const page = params?.page || 1;
  const pageSize = params?.pageSize || 20;
  const skip = (page - 1) * pageSize;

  const [items, total] = await Promise.all([
    collection
      .find(query)
      .sort({ created_at: -1 })
      .skip(skip)
      .limit(pageSize)
      .toArray(),
    collection.countDocuments(query),
  ]);

  return {
    items: items.map((item: any) => ({
      _id: item._id?.toString() || '',
      workflow_id: item.workflow_id,
      workflow_type: item.workflow_type,
      status: item.status || 'pending',
      created_at: item.created_at,
      updated_at: item.updated_at,
      eligibility_result: item.eligibility_result || null,
      case_count: item.case_count || 1,
      processed: item.processed || 0,
    })),
    total,
    page,
    pageSize,
  };
}

export async function getGatewaySubmission(id: string) {
  const { db } = await connectToDatabase();
  const collection = db.collection('gateway_submissions');

  const ObjectId = (await import('mongodb')).ObjectId;
  const item = await collection.findOne({ _id: new ObjectId(id) });

  if (!item) {
    return null;
  }

  return {
    _id: item._id?.toString() || '',
    workflow_id: item.workflow_id,
    workflow_type: item.workflow_type,
    status: item.status || 'pending',
    created_at: item.created_at,
    updated_at: item.updated_at,
    eligibility_result: item.eligibility_result || null,
    case_count: item.case_count || 1,
    processed: item.processed || 0,
  };
}

export async function deleteGatewaySubmission(id: string) {
  const { db } = await connectToDatabase();
  const collection = db.collection('gateway_submissions');

  const ObjectId = (await import('mongodb')).ObjectId;
  const result = await collection.deleteOne({ _id: new ObjectId(id) });

  return result.deletedCount > 0;
}
