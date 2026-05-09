import { AccountCategory } from '@ghostfolio/common/enums';
import { IsCurrencyCode } from '@ghostfolio/common/validators/is-currency-code';

import { Transform, TransformFnParams } from 'class-transformer';
import {
  IsBoolean,
  IsEnum,
  IsNumber,
  IsOptional,
  IsString,
  ValidateIf
} from 'class-validator';
import { isString } from 'lodash';

export class CreateAccountDto {
  @IsNumber()
  balance: number;

  @IsEnum(AccountCategory)
  @IsOptional()
  category?: AccountCategory;

  @IsOptional()
  @IsString()
  @Transform(({ value }: TransformFnParams) =>
    isString(value) ? value.trim() : value
  )
  comment?: string;

  @IsCurrencyCode()
  currency: string;

  @IsOptional()
  @IsString()
  id?: string;

  @IsBoolean()
  @IsOptional()
  isExcluded?: boolean;

  @IsString()
  name: string;

  @IsString()
  @ValidateIf((_object, value) => value !== null)
  platformId: string | null;
}
