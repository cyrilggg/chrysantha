import { NUMERICAL_PRECISION_THRESHOLD_6_FIGURES } from '@ghostfolio/common/config';
import { User } from '@ghostfolio/common/interfaces';
import { GfValueComponent } from '@ghostfolio/ui/value';

import {
  ChangeDetectionStrategy,
  Component,
  Input,
  OnChanges,
  SimpleChanges
} from '@angular/core';
import { MatCardModule } from '@angular/material/card';
import { IonIcon } from '@ionic/angular/standalone';
import { addIcons } from 'ionicons';
import { shieldCheckmarkOutline } from 'ionicons/icons';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [GfValueComponent, IonIcon, MatCardModule],
  selector: 'gf-home-buffer',
  styleUrls: ['./home-buffer.scss'],
  templateUrl: './home-buffer.html'
})
export class GfHomeBufferComponent implements OnChanges {
  @Input() baseCurrency: string;
  @Input() deviceType: string;
  @Input() locale: string;
  @Input() user: User;

  public bufferAccounts: { balance: number; currency: string; name: string }[] =
    [];
  public bufferTotal: number;
  public hasBufferAccounts = false;
  public precision = 2;

  public constructor() {
    addIcons({ shieldCheckmarkOutline });
  }

  public ngOnChanges(changes: SimpleChanges) {
    if (changes.user && this.user?.accounts) {
      this.computeBuffer();
    }
  }

  private computeBuffer() {
    this.bufferAccounts = this.user.accounts
      .filter(
        (account) =>
          account.isExcluded ||
          account.name?.startsWith('Cash_') ||
          account.comment?.includes('#Reserve')
      )
      .map((account) => ({
        balance: account.balance,
        currency: account.currency,
        name: account.name
      }));

    this.hasBufferAccounts = this.bufferAccounts.length > 0;
    this.bufferTotal = this.bufferAccounts.reduce(
      (sum, acc) => sum + acc.balance,
      0
    );
  }
}
