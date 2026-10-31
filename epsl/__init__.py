import pymysql

# Compatibilidade do driver PyMySQL com Django 5.x para o ambiente de testes
pymysql.version_info = (2, 2, 7, "final", 0)
pymysql.install_as_MySQLdb()
