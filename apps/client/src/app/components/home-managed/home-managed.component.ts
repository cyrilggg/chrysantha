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
import { peopleOutline } from 'ionicons/icons';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [GfValueComponent, IonIcon, MatCardModule],
  selector: 'gf-home-managed',
  styleUrls: ['./home-managed.scss'],
  templateUrl: './home-managed.html'
})
export class GfHomeManagedComponent implements OnChanges {
  @Input() baseCurrency: string;
  @Input() deviceType: string;
  @Input() locale: string;
  @Input() user: User;

  public hasManagedAccounts = false;
  public managedAccounts: {
    balance: number;
    currency: string;
    name: string;
  }[] = [];
  public managedTotal: number;
  public precision = 2;

  public constructor() {
    addIcons({ peopleOutline });
  }

  public ngOnChanges(changes: SimpleChanges) {
    if (changes.user && this.user?.accounts) {
      this.computeManaged();
    }
  }

  private computeManaged() {
    this.managedAccounts = this.user.accounts
      .filter(
        (account) =>
          account.name?.startsWith('Mom_') ||
          account.name?.startsWith('Dad_') ||
          account.name?.startsWith('Parent_') ||
          account.comment?.includes('#ForMom') ||
          account.comment?.includes('#ForFamily')
      )
      .map((account) => ({
        balance: account.balance,
        currency: account.currency,
        name: account.name
      }));

    this.hasManagedAccounts = this.managedAccounts.length > 0;
    this.managedTotal = this.managedAccounts.reduce(
      (sum, acc) => sum + acc.balance,
      0
    );
  }
}
