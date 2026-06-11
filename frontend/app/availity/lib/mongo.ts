import { MongoClient } from "mongodb";

const uri = process.env.MONGO_URI_PROD;
const dbName = process.env.MONGO_DB_PROD;

if (!uri) {
  throw new Error("MONGO_URI_PROD not configured");
}

if (!dbName) {
  throw new Error("MONGO_DB_PROD not configured");
}

let client: MongoClient;
let clientPromise: Promise<MongoClient>;

declare global {
  var _mongoClientPromise: Promise<MongoClient> | undefined;
}

if (process.env.NODE_ENV === "development") {
  if (!global._mongoClientPromise) {
    client = new MongoClient(uri);
    global._mongoClientPromise = client.connect();
  }

  clientPromise = global._mongoClientPromise;
} else {
  client = new MongoClient(uri);
  clientPromise = client.connect();
}

export async function getDb() {
  const client = await clientPromise;
  return client.db(dbName);
}