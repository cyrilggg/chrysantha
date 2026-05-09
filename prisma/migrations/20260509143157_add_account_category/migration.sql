-- CreateEnum
CREATE TYPE "AccountCategory" AS ENUM ('BANK', 'CREDIT', 'INVESTMENT', 'OTHER', 'PAYMENT');

-- AlterTable
ALTER TABLE "Account" ADD COLUMN     "category" "AccountCategory" DEFAULT 'INVESTMENT';

-- CreateIndex
CREATE INDEX "Account_category_idx" ON "Account"("category");
