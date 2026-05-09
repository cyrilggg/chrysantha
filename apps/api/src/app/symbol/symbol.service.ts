import { DataProviderService } from '@ghostfolio/api/services/data-provider/data-provider.service';
import { DataGatheringItem } from '@ghostfolio/api/services/interfaces/interfaces';
import { MarketDataService } from '@ghostfolio/api/services/market-data/market-data.service';
import { PrismaService } from '@ghostfolio/api/services/prisma/prisma.service';
import { DATE_FORMAT } from '@ghostfolio/common/helper';
import {
  DataProviderHistoricalResponse,
  HistoricalDataItem,
  LookupItem,
  LookupResponse,
  SymbolItem
} from '@ghostfolio/common/interfaces';
import { UserWithSettings } from '@ghostfolio/common/types';

import { Injectable, Logger } from '@nestjs/common';
import { AssetClass, AssetSubClass, DataSource } from '@prisma/client';
import { format, subDays } from 'date-fns';

@Injectable()
export class SymbolService {
  public constructor(
    private readonly dataProviderService: DataProviderService,
    private readonly marketDataService: MarketDataService,
    private readonly prismaService: PrismaService
  ) {}

  public async get({
    dataGatheringItem,
    includeHistoricalData
  }: {
    dataGatheringItem: DataGatheringItem;
    includeHistoricalData?: number;
  }): Promise<SymbolItem> {
    const quotes = await this.dataProviderService.getQuotes({
      items: [dataGatheringItem]
    });
    const { currency, marketPrice } = quotes[dataGatheringItem.symbol] ?? {};

    if (dataGatheringItem.dataSource && marketPrice >= 0) {
      let historicalData: HistoricalDataItem[] = [];

      if (includeHistoricalData > 0) {
        const days = includeHistoricalData;

        const marketData = await this.marketDataService.getRange({
          assetProfileIdentifiers: [
            {
              dataSource: dataGatheringItem.dataSource,
              symbol: dataGatheringItem.symbol
            }
          ],
          dateQuery: { gte: subDays(new Date(), days) }
        });

        historicalData = marketData.map(({ date, marketPrice: value }) => {
          return {
            value,
            date: date.toISOString()
          };
        });
      }

      return {
        currency,
        historicalData,
        marketPrice,
        dataSource: dataGatheringItem.dataSource,
        symbol: dataGatheringItem.symbol
      };
    }

    return undefined;
  }

  public async getForDate({
    dataSource,
    date = new Date(),
    symbol
  }: DataGatheringItem): Promise<DataProviderHistoricalResponse> {
    let historicalData: {
      [symbol: string]: {
        [date: string]: DataProviderHistoricalResponse;
      };
    } = {
      [symbol]: {}
    };

    try {
      historicalData = await this.dataProviderService.getHistoricalRaw({
        assetProfileIdentifiers: [{ dataSource, symbol }],
        from: date,
        to: date
      });
    } catch {}

    return {
      marketPrice:
        historicalData?.[symbol]?.[format(date, DATE_FORMAT)]?.marketPrice
    };
  }

  public async lookup({
    includeIndices = false,
    query,
    user
  }: {
    includeIndices?: boolean;
    query: string;
    user: UserWithSettings;
  }): Promise<LookupResponse> {
    const results: LookupResponse = { items: [] };

    if (!query) {
      return results;
    }

    try {
      const { items } = await this.dataProviderService.search({
        includeIndices,
        query,
        user
      });
      results.items = items;
    } catch (error) {
      Logger.error(error, 'SymbolService');
    }

    // A-share fallback: if no results from providers, try to match and
    // auto-create a MANUAL SymbolProfile for Chinese ticker codes
    if (results.items.length === 0) {
      const aShareItem = await this.matchAShareSymbol(query);
      if (aShareItem) {
        results.items.push(aShareItem);
      }
    }

    return results;
  }

  /**
   * Matches a query against A-share ticker patterns and creates a MANUAL
   * SymbolProfile if one doesn't exist yet.
   *
   * Recognized formats:
   *   - SH600519  (Shanghai, explicit prefix)
   *   - SZ002594  (Shenzhen, explicit prefix)
   *   - 513100    (bare 6 digits starting with 0/3/5/6 → Shanghai ETF/stock)
   *   - 002594    (bare 6 digits starting with 00/002 → Shenzhen)
   *   - 300750    (bare 6 digits starting with 30 → Shenzhen ChiNext)
   */
  private async matchAShareSymbol(query: string): Promise<LookupItem | null> {
    const trimmed = query.trim().toUpperCase();

    // Already has exchange prefix
    const explicitMatch = /^(SH|SZ)(\d{6})$/.exec(trimmed);
    if (explicitMatch) {
      const exchange = explicitMatch[1];
      const code = explicitMatch[2];
      const symbol = `${exchange}${code}`;
      return this.getOrCreateManualSymbol(symbol, code);
    }

    // Bare 6-digit code — infer exchange
    const bareMatch = /^(\d{6})$/.exec(trimmed);
    if (bareMatch) {
      const code = bareMatch[1];
      const exchange = this.inferExchange(code);
      if (!exchange) return null;
      const symbol = `${exchange}${code}`;
      return this.getOrCreateManualSymbol(symbol, code);
    }

    return null;
  }

  /**
   * Infer the exchange for a bare 6-digit A-share code.
   */
  private inferExchange(code: string): string | null {
    const prefix = code.substring(0, 2);
    const firstDigit = code[0];

    // Shanghai: 5xxxxx (ETFs, funds), 6xxxxx (stocks), 0xxxxx (indices)
    if (['5', '6', '0'].includes(firstDigit)) return 'SH';

    // Shenzhen: 00xxxx, 002xxx, 30xxxx (ChiNext)
    if (prefix === '00' || prefix === '30') return 'SZ';

    return null;
  }

  /**
   * Finds or creates a MANUAL SymbolProfile for the given symbol.
   */
  private async getOrCreateManualSymbol(
    symbol: string,
    code: string
  ): Promise<LookupItem | null> {
    try {
      // Check if already exists
      let profile = await this.prismaService.symbolProfile.findUnique({
        where: { dataSource_symbol: { dataSource: DataSource.MANUAL, symbol } }
      });

      if (!profile) {
        // Create a new MANUAL profile
        profile = await this.prismaService.symbolProfile.create({
          data: {
            assetClass: 'EQUITY',
            assetSubClass:
              symbol.startsWith('SH5') || symbol.startsWith('SZ1')
                ? 'ETF'
                : 'STOCK',
            currency: 'CNY',
            dataSource: DataSource.MANUAL,
            name: code,
            symbol
          }
        });
      }

      return {
        assetClass: (profile.assetClass ?? 'EQUITY') as AssetClass,
        assetSubClass: (profile.assetSubClass ?? 'STOCK') as AssetSubClass,
        currency: 'CNY',
        dataProviderInfo: {
          isPremium: false,
          name: 'Manual'
        },
        dataSource: DataSource.MANUAL,
        name: profile.name ?? code,
        symbol
      };
    } catch (error) {
      Logger.error(
        `Could not create MANUAL symbol profile for ${symbol}: ${error.message}`,
        'SymbolService'
      );
      return null;
    }
  }

  public async getManualAShares() {
    const profiles = await this.prismaService.symbolProfile.findMany({
      where: { dataSource: DataSource.MANUAL },
      select: { symbol: true, name: true, currency: true }
    });

    const aSharePattern = /^(SH|SZ)\d{6}$/i;

    return profiles
      .filter((p) => aSharePattern.test(p.symbol))
      .map((p) => ({
        currency: p.currency,
        name: p.name ?? p.symbol,
        symbol: p.symbol
      }));
  }
}
