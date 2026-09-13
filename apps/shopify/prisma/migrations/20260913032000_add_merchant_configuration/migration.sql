CREATE TABLE "MerchantConfiguration" (
    "shop" TEXT NOT NULL PRIMARY KEY,
    "revision" TEXT NOT NULL,
    "asrProfileId" TEXT NOT NULL,
    "llmProfileId" TEXT NOT NULL,
    "ttsProfileId" TEXT NOT NULL,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" DATETIME NOT NULL
);

CREATE UNIQUE INDEX "MerchantConfiguration_revision_key"
ON "MerchantConfiguration"("revision");

CREATE TABLE "MerchantConfigurationRevision" (
    "revision" TEXT NOT NULL PRIMARY KEY,
    "shop" TEXT NOT NULL,
    "asrProfileId" TEXT NOT NULL,
    "llmProfileId" TEXT NOT NULL,
    "ttsProfileId" TEXT NOT NULL,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX "MerchantConfigurationRevision_shop_createdAt_idx"
ON "MerchantConfigurationRevision"("shop", "createdAt");
