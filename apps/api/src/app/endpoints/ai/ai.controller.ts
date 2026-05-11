import { HasPermission } from '@ghostfolio/api/decorators/has-permission.decorator';
import { HasPermissionGuard } from '@ghostfolio/api/guards/has-permission.guard';
import { ApiService } from '@ghostfolio/api/services/api/api.service';
import { AiPromptResponse } from '@ghostfolio/common/interfaces';
import { permissions } from '@ghostfolio/common/permissions';
import type { AiPromptMode, RequestWithUser } from '@ghostfolio/common/types';

import {
  Body,
  Controller,
  Get,
  Inject,
  Param,
  Post,
  Query,
  UseGuards
} from '@nestjs/common';
import { REQUEST } from '@nestjs/core';
import { AuthGuard } from '@nestjs/passport';

import { AiService } from './ai.service';

@Controller('ai')
export class AiController {
  public constructor(
    private readonly aiService: AiService,
    private readonly apiService: ApiService,
    @Inject(REQUEST) private readonly request: RequestWithUser
  ) {}

  @Get('prompt/:mode')
  @HasPermission(permissions.readAiPrompt)
  @UseGuards(AuthGuard('jwt'), HasPermissionGuard)
  public async getPrompt(
    @Param('mode') mode: AiPromptMode,
    @Query('accounts') filterByAccounts?: string,
    @Query('assetClasses') filterByAssetClasses?: string,
    @Query('dataSource') filterByDataSource?: string,
    @Query('symbol') filterBySymbol?: string,
    @Query('tags') filterByTags?: string
  ): Promise<AiPromptResponse> {
    const filters = this.apiService.buildFiltersFromQueryParams({
      filterByAccounts,
      filterByAssetClasses,
      filterByDataSource,
      filterBySymbol,
      filterByTags
    });

    const prompt = await this.aiService.getPrompt({
      filters,
      mode,
      impersonationId: undefined,
      languageCode: this.request.user.settings.settings.language,
      userCurrency: this.request.user.settings.settings.baseCurrency,
      userId: this.request.user.id
    });

    return { prompt };
  }

  @Post('trading-analysis/:dataSource/:symbol')
  @HasPermission(permissions.readAiPrompt)
  @UseGuards(AuthGuard('jwt'), HasPermissionGuard)
  public async getTradingAnalysis(
    @Param('dataSource') _dataSource: string,
    @Param('symbol') symbol: string,
    @Query('date') date?: string,
    @Query('debateRounds') debateRounds?: string
  ) {
    const analysisDate = date || new Date().toISOString().split('T')[0];

    return this.aiService.callTradingBridge({
      ticker: symbol,
      date: analysisDate,
      debateRounds: debateRounds ? parseInt(debateRounds, 10) : 1
    });
  }

  @Post('execute')
  @HasPermission(permissions.readAiPrompt)
  @UseGuards(AuthGuard('jwt'), HasPermissionGuard)
  public async executeTrade(@Body() body: {
    ticker: string;
    dataSource?: string;
    date?: string;
    signal: string;
    decision: Record<string, unknown>;
    reports?: Record<string, unknown>;
    quantity?: number;
    price?: number;
    orderType?: string;
    stopLoss?: number;
    accountId?: string;
    dryRun?: boolean;
  }) {
    return this.aiService.callExecutorBridge('/execute', {
      ticker: body.ticker,
      data_source: body.dataSource || 'MANUAL',
      date: body.date || new Date().toISOString().split('T')[0],
      signal: body.signal,
      decision: body.decision,
      reports: body.reports || {},
      quantity: body.quantity ?? null,
      price: body.price ?? null,
      order_type: body.orderType || 'LIMIT',
      stop_loss: body.stopLoss ?? null,
      account_id: body.accountId ?? null,
      dry_run: body.dryRun || false
    });
  }

  @Post('auto-execute/:dataSource/:symbol')
  @HasPermission(permissions.readAiPrompt)
  @UseGuards(AuthGuard('jwt'), HasPermissionGuard)
  public async autoExecute(
    @Param('dataSource') dataSource: string,
    @Param('symbol') symbol: string,
    @Query('date') date?: string,
    @Query('debateRounds') debateRounds?: string,
    @Query('riskRounds') riskRounds?: string,
    @Query('confidenceThreshold') confidenceThreshold?: string,
    @Query('maxPositionPct') maxPositionPct?: string,
    @Query('accountId') accountId?: string,
    @Query('dryRun') dryRun?: string
  ) {
    const analysisDate = date || new Date().toISOString().split('T')[0];

    return this.aiService.callExecutorBridge('/auto-execute', {
      ticker: symbol,
      data_source: dataSource,
      date: analysisDate,
      confidence_threshold: confidenceThreshold ? parseFloat(confidenceThreshold) : 0.7,
      max_position_pct: maxPositionPct ? parseFloat(maxPositionPct) : 0.1,
      debate_rounds: debateRounds ? parseInt(debateRounds, 10) : 1,
      risk_rounds: riskRounds ? parseInt(riskRounds, 10) : 1,
      account_id: accountId ?? null,
      dry_run: dryRun === 'true'
    });
  }

  @Get('execution/:id/status')
  @HasPermission(permissions.readAiPrompt)
  @UseGuards(AuthGuard('jwt'), HasPermissionGuard)
  public async getExecutionStatus(@Param('id') executionId: string) {
    return this.aiService.getExecutionStatus(executionId);
  }
}
