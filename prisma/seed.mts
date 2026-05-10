import { createHmac, pbkdf2Sync } from 'node:crypto';
import { PrismaPg } from '@prisma/adapter-pg';
import { PrismaClient } from '@prisma/client';

const adapter = new PrismaPg({
  connectionString: process.env.DATABASE_URL
});

const prisma = new PrismaClient({ adapter });

const LOCAL_DEV_USER_ID = '00000000-0000-0000-0000-000000000001';

async function main() {
  // Default tags
  await prisma.tag.createMany({
    data: [
      {
        id: '4452656d-9fa4-4bd0-ba38-70492e31d180',
        name: 'EMERGENCY_FUND'
      },
      {
        id: 'f2e868af-8333-459f-b161-cbc6544c24bd',
        name: 'EXCLUDE_FROM_ANALYSIS'
      }
    ],
    skipDuplicates: true
  });

  // Auto-create local dev user with access token from GHOSTFOLIO_ACCESS_TOKEN env
  const accessToken = process.env.GHOSTFOLIO_ACCESS_TOKEN;
  const hasAccessToken = accessToken && accessToken !== '<INSERT_YOUR_GHOSTFOLIO_ACCESS_TOKEN>';

  if (hasAccessToken) {
    const existingUser = await prisma.user.findUnique({
      where: { id: LOCAL_DEV_USER_ID }
    });

    if (!existingUser) {
      const salt = process.env.ACCESS_TOKEN_SALT || 'dev-salt';
      const hashedAccessToken = createHmac('sha512', salt)
        .update(accessToken)
        .digest('hex');

      await prisma.user.create({
        data: {
          id: LOCAL_DEV_USER_ID,
          accessToken: hashedAccessToken,
          role: 'ADMIN',
          accounts: {
            create: {
              currency: 'CNY',
              name: 'My Account'
            }
          },
          settings: {
            create: {
              settings: { currency: 'CNY' }
            }
          }
        }
      });

      console.log(
        `[seed] Local dev user created (id: ${LOCAL_DEV_USER_ID}) with access token from GHOSTFOLIO_ACCESS_TOKEN`
      );
    } else if (!existingUser.accessToken) {
      // User exists but no access token — update it
      const salt = process.env.ACCESS_TOKEN_SALT || 'dev-salt';
      const hashedAccessToken = createHmac('sha512', salt)
        .update(accessToken)
        .digest('hex');

      await prisma.user.update({
        data: { accessToken: hashedAccessToken },
        where: { id: LOCAL_DEV_USER_ID }
      });

      console.log(`[seed] Updated local dev user's access token`);
    } else {
      console.log('[seed] Local dev user already exists with access token');
    }

    // Auto-create API key for local dev (used by chrysantha-sync via Authorization: Api-Key)
    const existingApiKey = await prisma.apiKey.findFirst({
      where: { userId: LOCAL_DEV_USER_ID }
    });

    if (!existingApiKey) {
      const hashedKey = pbkdf2Sync(
        accessToken,
        '',
        100000,
        64,
        'sha256'
      ).toString('hex');

      await prisma.apiKey.create({
        data: {
          hashedKey,
          userId: LOCAL_DEV_USER_ID
        }
      });

      console.log('[seed] API key created for local dev user');
    } else {
      console.log('[seed] API key already exists for local dev user');
    }
  } else {
    console.log('[seed] Skipping user seed: GHOSTFOLIO_ACCESS_TOKEN not set');
  }
}

main()
  .catch((e) => {
    console.error(e);
    process.exit(1);
  })
  .finally(async () => {
    await prisma.$disconnect();
  });
