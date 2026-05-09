import { AccountCategory } from '@ghostfolio/common/enums';
import { getBackgroundColor, getLocale, getTextColor } from '@ghostfolio/common/helper';
import { AccountWithValue } from '@ghostfolio/common/types';

import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Input,
  OnChanges,
  OnDestroy,
  ViewChild
} from '@angular/core';
import {
  ArcElement,
  Chart,
  ChartData,
  DoughnutController,
  Legend,
  Tooltip
} from 'chart.js';
import { NgxSkeletonLoaderModule } from 'ngx-skeleton-loader';

const CATEGORY_LABELS: Record<AccountCategory, string> = {
  [AccountCategory.BANK]: $localize`Bank`,
  [AccountCategory.CREDIT]: $localize`Credit Card`,
  [AccountCategory.INVESTMENT]: $localize`Investment`,
  [AccountCategory.OTHER]: $localize`Other`,
  [AccountCategory.PAYMENT]: $localize`Payment`
};

const CATEGORY_COLORS: Record<AccountCategory, string> = {
  [AccountCategory.BANK]: 'rgba(54, 162, 235, 0.8)',
  [AccountCategory.CREDIT]: 'rgba(255, 99, 132, 0.8)',
  [AccountCategory.INVESTMENT]: 'rgba(75, 192, 192, 0.8)',
  [AccountCategory.OTHER]: 'rgba(153, 102, 255, 0.8)',
  [AccountCategory.PAYMENT]: 'rgba(255, 159, 64, 0.8)'
};

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgxSkeletonLoaderModule],
  selector: 'gf-account-distribution-chart',
  styleUrls: ['./account-distribution-chart.component.scss'],
  templateUrl: './account-distribution-chart.component.html'
})
export class GfAccountDistributionChartComponent implements OnChanges, OnDestroy {
  @Input() accounts: AccountWithValue[];
  @Input() baseCurrency: string;
  @Input() isLoading = false;
  @Input() locale = getLocale();

  @ViewChild('chartCanvas') chartCanvas: ElementRef<HTMLCanvasElement>;

  private chart: Chart<'doughnut'>;
  private chartData: ChartData<'doughnut'>;

  public constructor() {
    Chart.register(ArcElement, DoughnutController, Legend, Tooltip);
  }

  public ngOnChanges() {
    if (this.isLoading) {
      return;
    }
    this.initialize();
  }

  public ngOnDestroy() {
    this.chart?.destroy();
  }

  private initialize() {
    if (this.chartCanvas) {
      this.generateChartData();
      this.renderChart();
    }
  }

  private generateChartData() {
    const categoryTotals = new Map<AccountCategory, number>();

    for (const account of this.accounts) {
      const cat = (account.category as AccountCategory) || AccountCategory.OTHER;
      const current = categoryTotals.get(cat) ?? 0;
      categoryTotals.set(cat, current + (account.valueInBaseCurrency || 0));
    }

    // Only include categories with non-zero value
    const entries = Array.from(categoryTotals.entries()).filter(
      ([, value]) => value > 0
    );

    this.chartData = {
      labels: entries.map(([cat]) => CATEGORY_LABELS[cat] || cat),
      datasets: [
        {
          backgroundColor: entries.map(([cat]) => CATEGORY_COLORS[cat] || 'rgba(200, 200, 200, 0.8)'),
          borderColor: entries.map(() => getBackgroundColor()),
          borderWidth: 2,
          data: entries.map(([, value]) => value)
        }
      ]
    };
  }

  private renderChart() {
    this.chart?.destroy();

    const ctx = this.chartCanvas.nativeElement.getContext('2d');
    if (!ctx) return;

    this.chart = new Chart(ctx, {
      type: 'doughnut',
      data: this.chartData,
      options: {
        cutout: '60%',
        plugins: {
          legend: {
            display: true,
            labels: { color: getTextColor() },
            position: 'bottom'
          },
          tooltip: {
            callbacks: {
              label: (context) => {
                const label = context.label || '';
                const value = context.parsed;
                const total = context.dataset.data.reduce(
                  (sum: number, v: number) => sum + v,
                  0
                );
                const pct = total > 0 ? ((value / total) * 100).toFixed(1) : '0';
                return `${label}: ${this.baseCurrency} ${value.toLocaleString(this.locale, { minimumFractionDigits: 2 })} (${pct}%)`;
              }
            }
          }
        }
      }
    });
  }
}
