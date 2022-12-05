# Copyright (C) 2020-2022 by the Free Software Foundation, Inc.
#
# This file is part of GNU Mailman.
#
# GNU Mailman is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# GNU Mailman is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# GNU Mailman.  If not, see <https://www.gnu.org/licenses/>.
"""add_archive_rendering_mode

Revision ID: 9735f5e5dbdb
Revises: d151c0b8d6f7
Create Date: 2020-05-14 15:59:26.277647

"""
import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision = '9735f5e5dbdb'
down_revision = 'd151c0b8d6f7'


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('mailinglist', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('archive_rendering_mode', sa.Integer(), nullable=True))
    # Default it to 'text' for all the MailingList since this is a new field.
    op.execute("UPDATE mailinglist SET archive_rendering_mode = 1")
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('mailinglist', schema=None) as batch_op:
        batch_op.drop_column('archive_rendering_mode')
    # ### end Alembic commands ###
